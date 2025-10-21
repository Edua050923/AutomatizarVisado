# bot_visado.py
# Versión final: paralela (Modo A) + resumen HTML oscuro cada 12 horas.
# Muestra el NOMBRE de la cuenta en logs, correos y resúmenes (si existe en config),
# manteniendo el identificador para uso interno (consultas y archivos).
#
# Requisitos: selenium, pillow, pytesseract, schedule, pyyaml
# Asegúrate de tener chromedriver compatible en PATH o que webdriver.Chrome() funcione en tu sistema.

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import time
import schedule
import logging
import yaml
import json
import os
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
import tempfile
import base64
from threading import Thread
from datetime import datetime, timedelta

class BotVisado:
    def __init__(self, config_path="config.yaml"):
        self.config = self.cargar_config(config_path)
        self.setup_logging()
        # Cargar lista de cuentas
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración.")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")
        # Nota: NO usamos self.driver global en esta versión; cada hilo crea su propio driver.

    def cargar_config(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('bot.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)

    # --- Helpers para nombres / display ---
    def _display_name(self, identificador):
        """
        Devuelve el nombre asociado a un identificador si existe en config,
        en caso contrario devuelve el identificador.
        """
        try:
            for cuenta in self.cuentas:
                if cuenta.get('identificador') == identificador:
                    nombre = cuenta.get('nombre')
                    if nombre:
                        return nombre
        except Exception:
            pass
        return identificador

    # Helper para logs que añaden el nombre al principio (si existe)
    def _log(self, nivel, identificador, mensaje):
        display = self._display_name(identificador)
        prefix = f"({display}) " if display else ""
        getattr(self.logger, nivel)(f"{prefix}{mensaje}")

    def inicializar_selenium(self):
        """Inicializa y devuelve un nuevo driver y wait (no asocia a self)."""
        try:
            options = webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--force-device-scale-factor=2")
            driver = webdriver.Chrome(options=options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            wait = WebDriverWait(driver, 15)  # Timeout general por operación
            return driver, wait
        except Exception as e:
            self.logger.error(f"Error al inicializar Selenium: {str(e)}")
            raise

    # --- CAPTCHA / OCR / Imagen ---
    def capturar_captcha(self, driver, wait, identificador=None):
        """Captura una imagen del CAPTCHA usando JavaScript (base64) y la guarda en temp."""
        try:
            captcha_element = wait.until(
                EC.visibility_of_element_located((By.ID, "imagenCaptcha"))
            )
            script = """
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.width;
            canvas.height = img.height;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png');
            """
            image_base64 = driver.execute_script(script, captcha_element)
            image_bytes = base64.b64decode(image_base64.split(',')[1])
            captcha_path = os.path.join(tempfile.gettempdir(), f"captcha_{int(time.time()*1000)}.png")
            with open(captcha_path, 'wb') as f:
                f.write(image_bytes)
            self._log('info', identificador, f"Imagen CAPTCHA capturada y guardada en: {captcha_path}")
            return captcha_path
        except Exception as e:
            self._log('error', identificador, f"Error al capturar el CAPTCHA: {e}")
            return None

    def preprocesar_captcha(self, image_path, identificador=None):
        """Preprocesa la imagen CAPTCHA para mejorar OCR (dígitos)."""
        try:
            image = Image.open(image_path)
            new_size = (image.width * 4, image.height * 4)
            image = image.resize(new_size, Image.LANCZOS)
            image = image.convert('L')  # escala de grises
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(4)
            image = image.point(lambda p: p > 150 and 255)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            processed_path = image_path.replace('.png', '_processed.png')
            image.save(processed_path)
            self._log('info', identificador, f"Imagen CAPTCHA preprocesada guardada en: {processed_path}")
            return processed_path
        except Exception as e:
            self._log('error', identificador, f"Error al preprocesar CAPTCHA: {e}")
            return image_path

    def resolver_captcha(self, image_path, identificador=None):
        try:
            image = Image.open(image_path)
            custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(image, config=custom_config).strip()
            texto_limpio = ''.join(c for c in texto if c.isdigit())
            self._log('info', identificador, f"Texto OCR del CAPTCHA (original): '{texto}'")
            self._log('info', identificador, f"Texto OCR del CAPTCHA (limpio): '{texto_limpio}'")
            if len(texto_limpio) == 6:  # Validar 6 dígitos (según tu config anterior)
                self._log('info', identificador, f"Texto OCR validado: '{texto_limpio}'")
                return texto_limpio
            else:
                self._log('warning', identificador, f"Texto OCR longitud inusual ({len(texto_limpio)}). Se considera inválido.")
                return ""
        except Exception as e:
            self._log('error', identificador, f"Error al resolver CAPTCHA con OCR: {e}")
            return ""

    # --- Interacción y extracción usando driver local ---
    def interactuar_con_formulario(self, driver, wait, identificador, ano_nacimiento, captcha_texto):
        try:
            tipo_tramite_select_element = wait.until(
                EC.element_to_be_clickable((By.ID, "infServicio"))
            )
            self._log('info', identificador, "Select 'infServicio' presente.")
            self.wait_for_option_visado(driver, wait)
            from selenium.webdriver.support.ui import Select
            select = Select(tipo_tramite_select_element)
            select.select_by_value("VISADO")

            identificador_input = wait.until(EC.presence_of_element_located((By.ID, "txIdentificador")))
            ano_nacimiento_input = wait.until(EC.presence_of_element_located((By.ID, "txtFechaNacimiento")))
            captcha_input = wait.until(EC.presence_of_element_located((By.ID, "imgcaptcha")))
            submit_button = wait.until(EC.element_to_be_clickable((By.ID, "imgVerSuTramite")))

            identificador_input.clear()
            identificador_input.send_keys(identificador)
            ano_nacimiento_input.clear()
            ano_nacimiento_input.send_keys(ano_nacimiento)
            captcha_input.clear()
            captcha_input.send_keys(captcha_texto)

            submit_button.click()
            self._log('info', identificador, f"Formulario enviado para {identificador}.")
            return True
        except (TimeoutException, NoSuchElementException) as e:
            self._log('error', identificador, f"Error al interactuar con el formulario: {e}")
            return False
        except Exception as e:
            self._log('error', identificador, f"Error inesperado interactuando con formulario: {e}")
            return False

    def wait_for_option_visado(self, driver, wait):
        # Helper sin logs (llamado desde interactuar_con_formulario) para esperar la opción VISADO
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//select[@id='infServicio']/option[@value='VISADO']"))
        )

    def extraer_estado(self, driver, wait, identificador=None):
        try:
            wait.until(EC.presence_of_element_located((By.ID, "CajaGenerica")))
            wait.until(
                lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip() != ""
            )
            wait.until(
                lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip() != ""
            )
            titulo_element = driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado")
            desc_element = driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado")
            titulo_estado = titulo_element.text.strip().upper()
            desc_estado = desc_element.text.strip()
            estado_completo = f"{titulo_estado} - {desc_estado}"
            self._log('info', identificador, f"Estado extraído: {estado_completo}")
            return estado_completo
        except (TimeoutException, NoSuchElementException) as e:
            self._log('error', identificador, f"Error al extraer el estado: {e}")
            try:
                error_captcha_element = driver.find_element(By.ID, "CompararCaptcha")
                if error_captcha_element.is_displayed():
                    error_text = error_captcha_element.text
                    self._log('warning', identificador, f"Mensaje de error del servidor (posible CAPTCHA incorrecto): {error_text}")
                    return None
            except NoSuchElementException:
                self._log('info', identificador, "No se encontró mensaje de error de CAPTCHA específico.")
            return None
        except Exception as e:
            self._log('error', identificador, f"Error inesperado al extraer estado: {e}")
            return None

    # --- Archivos por cuenta (estado / historial) ---
    def _get_estado_file(self, identificador):
        return f"estado_tramite_{identificador}.json"

    def _get_historial_file(self, identificador):
        return f"historial_verificaciones_{identificador}.json"

    def guardar_estado(self, identificador, estado):
        estado_file = self._get_estado_file(identificador)
        with open(estado_file, 'w', encoding='utf-8') as f:
            json.dump({"ultimo_estado": estado, "timestamp": time.time()}, f, ensure_ascii=False, indent=4)
        self._log('info', identificador, f"Estado guardado en {estado_file}: {estado}")

    def cargar_estado_anterior(self, identificador):
        estado_file = self._get_estado_file(identificador)
        estado_anterior = None
        if os.path.exists(estado_file):
            try:
                with open(estado_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    estado_anterior = data.get("ultimo_estado")
                    self._log('info', identificador, f"Estado anterior cargado: {estado_anterior}")
            except (json.JSONDecodeError, KeyError) as e:
                self._log('error', identificador, f"Error al cargar estado anterior: {e}")
        else:
            self._log('info', identificador, "Archivo de estado anterior no encontrado.")
        return estado_anterior

    def cargar_historial(self, identificador):
        historial_file = self._get_historial_file(identificador)
        historial = []
        if os.path.exists(historial_file):
            try:
                with open(historial_file, 'r', encoding='utf-8') as f:
                    historial = json.load(f)
                    self._log('info', identificador, f"Historial cargado. Total entradas: {len(historial)}")
            except (json.JSONDecodeError, KeyError) as e:
                self._log('error', identificador, f"Error al cargar historial: {e}")
        else:
            self._log('info', identificador, "Archivo de historial no encontrado.")
        return historial

    def guardar_historial(self, identificador, historial):
        historial_file = self._get_historial_file(identificador)
        with open(historial_file, 'w', encoding='utf-8') as f:
            json.dump(historial, f, ensure_ascii=False, indent=4)
        self._log('info', identificador, f"Historial guardado en {historial_file}. Total entradas: {len(historial)}")

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        historial = self.cargar_historial(identificador)
        entrada = {
            "fecha_hora": time.strftime('%Y-%m-%d %H:%M:%S'),
            "estado": estado,
            "exitoso": exitoso
        }
        historial.append(entrada)
        self.guardar_historial(identificador, historial)

    # --- Notificaciones por cuenta ---
    def _get_email_destino(self, identificador):
        for cuenta in self.cuentas:
            if cuenta.get('identificador') == identificador:
                return cuenta.get('email_notif', self.config['notificaciones'].get('email_destino'))
        return self.config['notificaciones'].get('email_destino')

    def enviar_notificacion(self, asunto, cuerpo, identificador_destino, es_html=False):
        """
        Envía una notificación. Si identificador_destino no corresponde a cuenta, se usa email_destino general.
        identificador_destino puede ser:
         - el identificador real de una cuenta -> se buscará su email y nombre para subject/log
         - "__RESUMEN__" -> se enviará al email_destino general (config.notificaciones.email_destino)
        """
        # Determinar email destino
        if identificador_destino == "__RESUMEN__":
            email_destino = self.config['notificaciones'].get('email_destino')
            display = "Resumen"
        else:
            email_destino = self._get_email_destino(identificador_destino)
            display = self._display_name(identificador_destino)

        if not email_destino:
            # Registrar error usando identificador_destino (si existe) o 'general'
            self._log('error', identificador_destino if identificador_destino != "__RESUMEN__" else "", f"No se encontró correo destino. No se envía notificación.")
            return
        try:
            # Ajustar asunto para mostrar el nombre (si corresponde) en lugar del identificador
            if identificador_destino != "__RESUMEN__":
                asunto = f"[BOT Visado] {self._display_name(identificador_destino)} - {asunto}"
            msg = MIMEMultipart('alternative' if es_html else 'mixed')
            msg['From'] = self.config['notificaciones']['email_origen']
            msg['To'] = email_destino
            msg['Subject'] = asunto

            if es_html:
                parte_html = MIMEText(cuerpo, 'html', 'utf-8')
                msg.attach(parte_html)
            else:
                parte_texto = MIMEText(cuerpo, 'plain', 'utf-8')
                msg.attach(parte_texto)

            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(self.config['notificaciones']['email_origen'], self.config['notificaciones']['app_password'])
            text = msg.as_string()
            server.sendmail(self.config['notificaciones']['email_origen'], email_destino, text)
            server.quit()
            # Log con nombre de la cuenta (si aplica)
            if identificador_destino == "__RESUMEN__":
                self.logger.info(f"(RESUMEN) Notificación enviada a {email_destino}: {asunto}")
            else:
                self._log('info', identificador_destino, f"Notificación enviada a {email_destino}: {asunto}")
        except Exception as e:
            if identificador_destino == "__RESUMEN__":
                self.logger.error(f"(RESUMEN) Error al enviar notificación: {e}")
            else:
                self._log('error', identificador_destino, f"Error al enviar notificación: {e}")

    # --- Consulta por cuenta usando driver local ---
    def consultar_estado_para_cuenta(self, driver, wait, identificador, ano_nacimiento):
        """Intenta múltiples reintentos del captcha y la consulta. Devuelve estado o None."""
        max_reintentos_captcha = 12
        intentos = 0
        while intentos < max_reintentos_captcha:
            self._log('info', identificador, f"Intento {intentos + 1} de {max_reintentos_captcha}")
            try:
                driver.get("https://sutramiteconsular.maec.es/  ")
                time.sleep(2 if intentos < 6 else 1)

                captcha_path = self.capturar_captcha(driver, wait, identificador)
                if not captcha_path:
                    intentos += 1
                    time.sleep(5)
                    continue

                processed_path = self.preprocesar_captcha(captcha_path, identificador)
                captcha_texto = self.resolver_captcha(processed_path, identificador)

                # Eliminar archivos temporales después del intento
                for path in [captcha_path, processed_path]:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass

                if not captcha_texto:
                    intentos += 1
                    time.sleep(5)
                    continue

                if not self.interactuar_con_formulario(driver, wait, identificador, ano_nacimiento, captcha_texto):
                    intentos += 1
                    time.sleep(5)
                    continue

                estado = self.extraer_estado(driver, wait, identificador)

                if estado is not None:
                    self.registrar_verificacion(identificador, estado, exitoso=True)
                    return estado
                else:
                    self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                    intentos += 1
                    time.sleep(5)
            except WebDriverException as e:
                self._log('error', identificador, f"Error de WebDriver durante la consulta: {e}")
                self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                intentos += 1
                time.sleep(5)
            except Exception as e:
                self._log('error', identificador, f"Error inesperado durante la consulta: {e}")
                self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                intentos += 1
                time.sleep(5)
        self._log('error', identificador, "Consulta fallida tras todos los reintentos.")
        return None

    # --- Worker por cuenta (usado por cada hilo) ---
    def worker_cuenta(self, cuenta):
        identificador = cuenta.get('identificador')
        nombre = cuenta.get('nombre', identificador)
        ano_nacimiento = cuenta.get('año_nacimiento')
        driver = None
        wait = None
        try:
            # Log inicial usa identificador pero _log lo mostrará como nombre si existe
            self._log('info', identificador, "Inicializando driver para la cuenta...")
            driver, wait = self.inicializar_selenium()
            estado_anterior = self.cargar_estado_anterior(identificador)
            estado_actual = self.consultar_estado_para_cuenta(driver, wait, identificador, ano_nacimiento)
            if estado_actual is not None:
                hay_cambio = estado_actual != estado_anterior
                es_primera_vez = estado_anterior is None

                if hay_cambio or es_primera_vez:
                    self.guardar_estado(identificador, estado_actual)
                    # Construir asunto/cuerpo con NOMBRE visible
                    display_name = self._display_name(identificador)
                    if es_primera_vez:
                        asunto = f"[BOT Visado] 🎉 Estado Inicial para {display_name}"
                        cuerpo = f"""
¡Hola! Este es el estado inicial de tu trámite para {display_name}.
Estado: {estado_actual}
Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}
Enlace: https://sutramiteconsular.maec.es/
El bot seguirá monitoreando.
"""
                    else:
                        asunto = f"[BOT Visado] 🚨 Cambio de Estado para {display_name}: {estado_actual}"
                        cuerpo = f"""
El estado de tu trámite para {display_name} ha cambiado.
Nuevo Estado: {estado_actual}
Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}
Enlace: https://sutramiteconsular.maec.es/
"""
                    self.enviar_notificacion(asunto, cuerpo, identificador)
                else:
                    self._log('info', identificador, f"Sin cambios: {estado_actual}")
            else:
                self._log('warning', identificador, "No se obtuvo estado válido.")
        except Exception as e:
            self._log('error', identificador, f"Error en worker_cuenta: {e}")
        finally:
            try:
                if driver:
                    driver.quit()
                    self._log('info', identificador, "Driver cerrado para la cuenta.")
            except Exception as e:
                self._log('warning', identificador, f"Error cerrando driver: {e}")

    # --- Resumen 12 horas (HTML oscuro) ---
    def generar_html_resumen_12h(self, resumen_global):
        """
        Genera un HTML con tema oscuro (estético) que presenta el resumen por cuenta.
        resumen_global: dict con keys: resumen_texto, tabla_rows (html), totals: {counts}
        """
        css = """
        body { background-color: #0f1724; color: #e6eef8; font-family: -apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,'Helvetica Neue',Arial; padding: 20px; }
        .card { background: linear-gradient(180deg, rgba(17,24,39,0.9), rgba(7,10,20,0.85)); border-radius: 12px; padding: 18px; box-shadow: 0 6px 18px rgba(2,6,23,0.6); }
        h1 { margin: 0 0 10px 0; font-size: 20px; color: #e6eef8; }
        .meta { color: #9fb3d6; font-size: 13px; margin-bottom: 12px; }
        table { width: 100%; border-collapse: collapse; margin-top: 12px; }
        th { text-align: left; padding: 10px; font-size: 13px; color: #cfe6ff; border-bottom: 1px solid rgba(255,255,255,0.06); }
        td { padding: 10px; font-size: 13px; border-bottom: 1px dashed rgba(255,255,255,0.03); color: #e6eef8; vertical-align: middle; }
        .ok { background: rgba(16,185,129,0.12); color: #a7f3d0; padding: 6px 10px; border-radius: 999px; display:inline-block; font-weight:600; }
        .err { background: rgba(239,68,68,0.12); color: #fecaca; padding: 6px 10px; border-radius: 999px; display:inline-block; font-weight:600; }
        .badge { font-size: 12px; color: #cfe6ff; background: rgba(255,255,255,0.03); padding: 4px 8px; border-radius: 8px; margin-left:8px; }
        .footer { margin-top: 14px; color: #93b0d6; font-size: 12px; }
        .header-row { display:flex; justify-content:space-between; align-items:center; gap:10px; }
        .small { font-size:12px; color:#9fb3d6; }
        @media (max-width:600px) { th, td { font-size:12px; padding:8px; } h1 { font-size:18px; } }
        """
        html = f"""<html><head><meta charset="utf-8"><style>{css}</style></head><body>
        <div class="card">
          <div class="header-row">
            <div>
              <h1>📊 Resumen de Monitoreo - Últimas 12 horas</h1>
              <div class="meta">{resumen_global.get('resumen_texto','')}</div>
            </div>
            <div class="small">
              <div><strong>Cuentas:</strong> {resumen_global['totals'].get('cuentas',0)}</div>
              <div><strong>Monitoreos:</strong> {resumen_global['totals'].get('monitoreos',0)}</div>
              <div><strong>Errores:</strong> {resumen_global['totals'].get('errores',0)}</div>
              <div style="margin-top:8px; font-size:12px; color:#bcd3f8;">Último ciclo: {resumen_global.get('ultimo_ciclo','-')}</div>
            </div>
          </div>

          <table role="presentation" cellspacing="0" cellpadding="0">
            <thead>
              <tr>
                <th>Hora</th>
                <th>Nombre</th>
                <th>Estado</th>
                <th>Resultado</th>
              </tr>
            </thead>
            <tbody>
              {resumen_global.get('tabla_rows','')}
            </tbody>
          </table>

          <div class="footer">
            Enviado por <strong>BOT Visado</strong> • {time.strftime('%Y-%m-%d %H:%M:%S')}
          </div>
        </div>
        </body></html>"""
        return html

    def enviar_resumen_12h(self):
        """
        Lee los historiales por cuenta, filtra las entradas de las últimas 12 horas,
        genera un HTML con tema oscuro y lo envía al email configurado.
        """
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=12)
            tabla_rows = []
            total_monitoreos = 0
            total_errores = 0
            cuentas_incluidas = 0
            ultimo_ciclo = time.strftime('%Y-%m-%d %H:%M:%S')

            for cuenta in self.cuentas:
                identificador = cuenta.get('identificador')
                nombre = cuenta.get('nombre', identificador)
                historial = self.cargar_historial(identificador)
                # Filtrar entradas en las últimas 12 horas
                recientes = []
                for entrada in historial:
                    fh = entrada.get('fecha_hora')
                    if not fh:
                        continue
                    try:
                        dt = datetime.strptime(fh, '%Y-%m-%d %H:%M:%S')
                    except Exception:
                        # si el formato varía, ignoramos esa entrada
                        continue
                    if dt >= cutoff:
                        recientes.append({"datetime": dt, "estado": entrada.get('estado'), "exitoso": entrada.get('exitoso', False)})
                if not recientes:
                    continue
                cuentas_incluidas += 1
                # Ordenar por datetime asc
                recientes = sorted(recientes, key=lambda x: x['datetime'])
                for r in recientes:
                    hora = r['datetime'].strftime('%Y-%m-%d %H:%M:%S')
                    estado = (r['estado'] or "").replace('\n',' ').strip()
                    exitoso = r.get('exitoso', False)
                    resultado_html = f"<span class='ok'>OK</span>" if exitoso else f"<span class='err'>ERROR</span>"
                    if not exitoso:
                        total_errores += 1
                    # Mostrar nombre (sin identificador) tal como pediste
                    tabla_rows.append(f"<tr><td>{hora}</td><td>{nombre}</td><td>{estado}</td><td>{resultado_html}</td></tr>")
                    total_monitoreos += 1

            resumen_texto = f"Resumen desde {cutoff.strftime('%Y-%m-%d %H:%M:%S')} hasta {now.strftime('%Y-%m-%d %H:%M:%S')}. Cuentas con actividad: {cuentas_incluidas}."
            resumen_global = {
                "resumen_texto": resumen_texto,
                "tabla_rows": "\n".join(tabla_rows) if tabla_rows else "<tr><td colspan='4' style='color:#9fb3d6;padding:12px;'>No se registraron monitoreos en las últimas 12 horas.</td></tr>",
                "totals": {
                    "cuentas": cuentas_incluidas,
                    "monitoreos": total_monitoreos,
                    "errores": total_errores
                },
                "ultimo_ciclo": ultimo_ciclo
            }

            html = self.generar_html_resumen_12h(resumen_global)
            asunto = f"[BOT Visado] Resumen de Monitoreo (Últimas 12h) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
            # Usamos enviar_notificacion enviando a la cuenta general (identificador_destino="__RESUMEN__")
            self.enviar_notificacion(asunto, html, identificador_destino="__RESUMEN__", es_html=True)
            self.logger.info("Resumen 12h generado y enviado.")
        except Exception as e:
            self.logger.error(f"Error generando/enviando resumen 12h: {e}")

    # --- Ejecución del monitoreo (paralelo por ciclo) ---
    def ejecutar_monitoreo(self):
        self.logger.info("Iniciando ciclo de monitoreo (paralelo) para múltiples cuentas.")
        hilos = []
        for cuenta in self.cuentas:
            identificador = cuenta.get('identificador')
            self._log('info', identificador, "Creando hilo de monitoreo para la cuenta...")
            hilo = Thread(target=self.worker_cuenta, args=(cuenta,), daemon=False)
            hilo.start()
            hilos.append(hilo)

        # Esperar a que terminen todos los hilos antes de terminar el ciclo
        for hilo in hilos:
            hilo.join()

        self.logger.info("Ciclo de monitoreo para todas las cuentas completado.")

    def iniciar(self):
        intervalo_horas = self.config.get('intervalo_horas', 0.5)
        intervalo_segundos = intervalo_horas * 3600
        # schedule espera un entero o float de segundos en .seconds
        schedule.every(intervalo_segundos).seconds.do(self.ejecutar_monitoreo)
        # Agregar resumen cada 12 horas
        schedule.every(12).hours.do(self.enviar_resumen_12h)
        self.logger.info(f"Monitoreo para {len(self.cuentas)} cuentas cada {intervalo_segundos/60:.1f} minutos. Resumen cada 12 horas.")
        # Primera ejecución inmediata
        self.ejecutar_monitoreo()
        # Bucle principal
        while True:
            schedule.run_pending()
            time.sleep(60)

    # Método de cierre general (por compatibilidad)
    def cerrar(self):
        # Nota: en este diseño los drivers se cierran por hilo (finally), así que aquí no hay driver global que cerrar.
        self.logger.info("Bot finalizado (no hay drivers globales que cerrar).")

if __name__ == "__main__":
    bot = BotVisado()
    try:
        bot.iniciar()
    except KeyboardInterrupt:
        print("\nInterrupción del usuario.")
        bot.logger.info("Cerrando bot...")
    except Exception as e:
        bot.logger.error(f"Error fatal: {e}")
    finally:
        bot.cerrar()
