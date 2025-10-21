# bot_visado.py (Optimizado)
# Versión con PostgreSQL + Resend para persistencia permanente y emails

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
import os
import tempfile
import base64
from threading import Thread
from datetime import datetime, timedelta
import requests
import json

# Importar el gestor de base de datos
from database import DatabaseManager

class BotVisado:
    def __init__(self, config_path="config.yaml"):
        self.config = self.cargar_config(config_path)
        self.setup_logging()
        
        # Inicializar base de datos PostgreSQL
        self.db = DatabaseManager()
        
        # Cargar lista de cuentas
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración.")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")

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
        """Preprocesa la imagen CAPTCHA para mejorar OCR (dígitos). OPTIMIZADO"""
        try:
            image = Image.open(image_path)
            
            # 1. Escalado (Aumento de resolución)
            new_size = (image.width * 4, image.height * 4)
            image = image.resize(new_size, Image.LANCZOS)
            
            # 2. Conversión a escala de grises
            image = image.convert('L')
            
            # 3. Aumento de Contraste
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(4)

            # 4. Desenfoque Gaussiano (Suavizar bordes y ruido fino)
            image = image.filter(ImageFilter.GaussianBlur(radius=1))
            
            # 5. Binarización (Umbral más estricto)
            # Experimenta con este valor (150 a 180) para el mejor resultado
            image = image.point(lambda p: p > 165 and 255) 
            
            # 6. Filtro de Mediana (Eliminar ruido 'salt and pepper')
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
            # Se cambia psm 8 a psm 6: 
            # psm 6: Assume a single uniform block of text. (Más robusto si hay espacio entre dígitos)
            custom_config = r'--oem 3 --psm 6 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(image, config=custom_config).strip()
            texto_limpio = ''.join(c for c in texto if c.isdigit())
            self._log('info', identificador, f"Texto OCR del CAPTCHA (original): '{texto}'")
            self._log('info', identificador, f"Texto OCR del CAPTCHA (limpio): '{texto_limpio}'")
            if len(texto_limpio) == 6:  # Validar 6 dígitos
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
            
            # Mejora: Simular mejor interacción humana (quitar el foco y pausa)
            driver.execute_script("arguments[0].blur();", captcha_input)
            time.sleep(0.5)

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

    # --- PERSISTENCIA EN POSTGRESQL (Sin cambios en este archivo, usa el gestor de DB) ---
    def guardar_estado(self, identificador, estado):
        """Guardar estado en PostgreSQL"""
        success = self.db.guardar_estado(identificador, estado)
        if success:
            self._log('info', identificador, f"Estado guardado en DB: {estado}")
        else:
            self._log('error', identificador, "Error guardando estado en DB")
        return success

    def cargar_estado_anterior(self, identificador):
        """Cargar estado anterior desde PostgreSQL"""
        estado = self.db.cargar_estado_anterior(identificador)
        if estado:
            self._log('info', identificador, f"Estado anterior cargado desde DB: {estado}")
        else:
            self._log('info', identificador, "No se encontró estado anterior en DB")
        return estado

    def cargar_historial(self, identificador):
        """Cargar historial desde PostgreSQL"""
        historial = self.db.cargar_historial(identificador)
        self._log('info', identificador, f"Historial cargado desde DB. Total entradas: {len(historial)}")
        return historial

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        """Registrar verificación en PostgreSQL"""
        success = self.db.registrar_verificacion(identificador, estado, exitoso)
        if success:
            self._log('info', identificador, f"Verificación registrada en DB: {estado} (exitoso: {exitoso})")
        else:
            self._log('error', identificador, "Error registrando verificación en DB")
        return success

    # --- NOTIFICACIONES CON RESEND (Sin cambios) ---
    def _get_email_destino(self, identificador):
        for cuenta in self.cuentas:
            if cuenta.get('identificador') == identificador:
                # Se utiliza el campo 'email_notif' si existe
                return cuenta.get('email_notif', self.config['notificaciones'].get('email_destinos')[0]) 
        # Si no se encuentra, se utiliza el primer email de la lista global
        return self.config['notificaciones'].get('email_destinos')[0] 

    def enviar_notificacion(self, asunto, cuerpo, identificador_destino, es_html=False):
        """
        Envía notificación usando Resend API.
        """
        # Determinar email destino
        if identificador_destino == "__RESUMEN__":
            # Envía al primer email de la lista de destinos para el resumen
            email_destino = self.config['notificaciones'].get('email_destinos')[0]
            display = "Resumen"
        else:
            email_destino = self._get_email_destino(identificador_destino)
            display = self._display_name(identificador_destino)

        if not email_destino:
            self._log('error', identificador_destino if identificador_destino != "__RESUMEN__" else "", 
                     "No se encontró correo destino.")
            return

        try:
            # Obtener API Key de Resend
            resend_api_key = os.environ.get('RESEND_API_KEY')
            
            if not resend_api_key:
                self._log('warning', identificador_destino, "RESEND_API_KEY no encontrada. Email no enviado.")
                self._log_simulado(asunto, cuerpo, identificador_destino, display)
                return

            # Configurar el email
            if identificador_destino != "__RESUMEN__":
                asunto_completo = f"[BOT Visado] {display} - {asunto}"
            else:
                asunto_completo = asunto

            # Enviar email usando Resend API
            response = requests.post(
                "https://api.resend.com/emails",
                headers={
                    "Authorization": f"Bearer {resend_api_key}",
                    "Content-Type": "application/json"
                },
                json={
                    "from": "BOT Visado <onboarding@resend.dev>",
                    "to": [email_destino],
                    "subject": asunto_completo,
                    "html": cuerpo if es_html else f"<pre style='font-family: Arial, sans-serif; white-space: pre-wrap;'>{cuerpo}</pre>"
                },
                timeout=30
            )

            # Verificar respuesta
            if response.status_code == 200:
                result = response.json()
                if identificador_destino == "__RESUMEN__":
                    self.logger.info(f"✅ (RESUMEN) Email enviado vía Resend a {email_destino}")
                else:
                    self._log('info', identificador_destino, f"✅ Email enviado vía Resend a {email_destino}")
            else:
                error_info = response.json()
                raise Exception(f"Resend API error: {response.status_code} - {error_info}")

        except Exception as e:
            if identificador_destino == "__RESUMEN__":
                self.logger.error(f"❌ (RESUMEN) Error enviando email: {e}")
            else:
                self._log('error', identificador_destino, f"Error enviando email: {e}")
            
            # Fallback: log simulado
            self._log_simulado(asunto, cuerpo, identificador_destino, display)

    def _log_simulado(self, asunto, cuerpo, identificador_destino, display):
        """Log cuando no se puede enviar email"""
        if identificador_destino == "__RESUMEN__":
            self.logger.info(f"📧 (SIMULADO RESUMEN) {asunto}")
            # Guardar resumen en un archivo temporal para debugging
            try:
                with open("resumen_simulado.html", "w", encoding="utf-8") as f:
                    f.write(cuerpo)
                self.logger.info("📄 Resumen guardado en resumen_simulado.html")
            except:
                pass
        else:
            self._log('info', identificador_destino, f"📧 (SIMULADO) {asunto}")

    # --- Consulta por cuenta usando driver local ---
    def consultar_estado_para_cuenta(self, driver, wait, identificador, ano_nacimiento):
        """Intenta múltiples reintentos del captcha y la consulta. Devuelve estado o None."""
        max_reintentos_captcha = 12
        intentos = 0
        while intentos < max_reintentos_captcha:
            self._log('info', identificador, f"Intento {intentos + 1} de {max_reintentos_captcha}")
            try:
                driver.get("https://sutramiteconsular.maec.es/  ")
                # Aumentar la pausa inicial para la carga estable de la página
                time.sleep(3) 

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

    # --- Worker, Resumen y Ejecución (Sin cambios) ---
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
                        asunto = f"🎉 Estado Inicial para {display_name}"
                        cuerpo = f"""
¡Hola! Este es el estado inicial de tu trámite para {display_name}.
Estado: {estado_actual}
Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}
Enlace: https://sutramiteconsular.maec.es/
El bot seguirá monitoreando.
"""
                    else:
                        asunto = f"🚨 Cambio de Estado para {display_name}: {estado_actual}"
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
        Lee los historiales por cuenta desde PostgreSQL, filtra las entradas de las últimas 12 horas,
        genera un HTML con tema oscuro y lo envía al email configurado.
        """
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=12)
            cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')
            
            tabla_rows = []
            total_monitoreos = 0
            total_errores = 0
            cuentas_incluidas = 0

            for cuenta in self.cuentas:
                identificador = cuenta.get('identificador')
                nombre = cuenta.get('nombre', identificador)
                
                # Cargar historial desde PostgreSQL
                historial = self.cargar_historial(identificador)
                
                # Filtrar entradas recientes
                recientes = []
                for entrada in historial:
                    fh = entrada.get('fecha_hora')
                    if not fh:
                        continue
                    try:
                        # Comparación directa de strings (formato YYYY-MM-DD HH:MM:SS)
                        if fh >= cutoff_str:
                            recientes.append(entrada)
                    except Exception:
                        continue
                
                if not recientes:
                    continue
                    
                cuentas_incluidas += 1
                for r in recientes:
                    hora = r.get('fecha_hora', '')
                    estado = (r.get('estado') or "").replace('\n',' ').strip()
                    exitoso = r.get('exitoso', False)
                    resultado_html = f"<span class='ok'>OK</span>" if exitoso else f"<span class='err'>ERROR</span>"
                    if not exitoso:
                        total_errores += 1
                    
                    tabla_rows.append(f"<tr><td>{hora}</td><td>{nombre}</td><td>{estado}</td><td>{resultado_html}</td></tr>")
                    total_monitoreos += 1

            resumen_texto = f"Resumen desde {cutoff_str} hasta {now.strftime('%Y-%m-%d %H:%M:%S')}. Cuentas con actividad: {cuentas_incluidas}."
            resumen_global = {
                "resumen_texto": resumen_texto,
                "tabla_rows": "\n".join(tabla_rows) if tabla_rows else "<tr><td colspan='4' style='color:#9fb3d6;padding:12px;'>No se registraron monitoreos en las últimas 12 horas.</td></tr>",
                "totals": {
                    "cuentas": len(self.cuentas), # Usar el total de cuentas configuradas
                    "monitoreos": total_monitoreos,
                    "errores": total_errores
                },
                "ultimo_ciclo": time.strftime('%Y-%m-%d %H:%M:%S')
            }

            html = self.generar_html_resumen_12h(resumen_global)
            asunto = f"Resumen de Monitoreo (Últimas 12h) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
            self.enviar_notificacion(asunto, html, identificador_destino="__RESUMEN__", es_html=True)
            self.logger.info("Resumen 12h generado y enviado desde PostgreSQL.")
            
        except Exception as e:
            self.logger.error(f"Error generando/enviando resumen 12h: {e}")

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
        # Cerrar conexión a la base de datos
        if hasattr(self, 'db'):
            self.db.close()
        self.logger.info("Bot finalizado. Conexión DB cerrada.")

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
