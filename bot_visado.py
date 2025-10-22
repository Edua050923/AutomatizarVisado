# bot_visado_unificado.py
# Fusionado: persistencia (Postgres) + Resend + OCR robusto + Selenium optimizado para Railway
# Basado en: bot_visado_final.py + bot_visado.py (archivos del usuario).

import os
import time
import base64
import yaml
import logging
import tempfile
import random
import schedule
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException

import pytesseract
from PIL import Image, ImageEnhance, ImageFilter

# Opcional: si tienes database.py con DatabaseManager (Postgres)
try:
    from database import DatabaseManager
    HAS_DB = True
except Exception:
    HAS_DB = False

# Opcional: requests para Resend
try:
    import requests
    HAS_REQUESTS = True
except Exception:
    HAS_REQUESTS = False

# ------------------ Clase unificada ------------------

class BotVisado:
    DEFAULT_MAX_CONCURRENCY = 4

    def __init__(self, config_path="config.yaml"):
        self.config = self._cargar_config(config_path)
        self._setup_logging()
        # Inicializar DB solo si está disponible y configurada
        self.db = None
        if HAS_DB and self.config.get('postgres', {}).get('enabled', False):
            try:
                self.db = DatabaseManager(self.config.get('postgres', {}))
                self.logger.info("Conexión a PostgreSQL establecida.")
            except Exception as e:
                self.logger.error(f"No se pudo conectar a PostgreSQL: {e}")
                self.db = None
        else:
            if not HAS_DB:
                self.logger.warning("No se encontró 'database.DatabaseManager'. Operando sin DB (modo simulado).")
            else:
                self.logger.info("Postgres deshabilitado en config. Operando sin DB.")

        # Cuentas
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración (config.yaml).")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")

        # Concurrencia
        self.MAX_CONCURRENCIA = self.config.get('max_concurrency', self.DEFAULT_MAX_CONCURRENCY)
        self.executor = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENCIA)

        # OCR config (permitir ajuste en config.yaml)
        ocr_conf = self.config.get('ocr', {})
        self.OCR_PSM = ocr_conf.get('psm', 8)
        self.OCR_OEM = ocr_conf.get('oem', 3)
        self.OCR_WHITELIST = ocr_conf.get('whitelist', '0123456789')
        self.OCR_MIN_LEN = ocr_conf.get('min_length', 6)
        self.MAX_REINTENTOS = self.config.get('max_reintentos', 15)

        # Resumen scheduling
        self.resumen_interval_hours = self.config.get('resumen_interval_hours', 12)
        self.monitoreo_interval_hours = self.config.get('intervalo_horas', 0.5)

    # ---------- Config / Logging ----------
    def _cargar_config(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def _setup_logging(self):
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('bot.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger("BotVisadoUnificado")

    # ---------- Helpers ----------
    def _display_name(self, identificador):
        for c in self.cuentas:
            if c.get('identificador') == identificador:
                return c.get('nombre') or identificador
        return identificador

    def _log(self, nivel, identificador, mensaje):
        prefix = f"({self._display_name(identificador)}) " if identificador else ""
        getattr(self.logger, nivel)(f"{prefix}{mensaje}")

    # DB wrappers (si no hay DB, operan en modo simulado / archivos)
    def guardar_estado(self, identificador, estado):
        if self.db:
            try:
                self.db.guardar_estado(identificador, estado)
                self._log('info', identificador, f"Estado guardado en DB: {estado}")
                return True
            except Exception as e:
                self._log('error', identificador, f"Error guardando en DB: {e}")
                return False
        else:
            # Simular: guardar en archivo local por identificador
            try:
                fname = f"estado_{identificador}.txt"
                with open(fname, 'w', encoding='utf-8') as f:
                    f.write(f"{estado}\n{time.strftime('%Y-%m-%d %H:%M:%S')}")
                self._log('info', identificador, f"Estado guardado localmente en {fname}")
                return True
            except Exception as e:
                self._log('error', identificador, f"Error guardando estado local: {e}")
                return False

    def cargar_estado_anterior(self, identificador):
        if self.db:
            try:
                estado = self.db.cargar_estado_anterior(identificador)
                return estado
            except Exception as e:
                self._log('error', identificador, f"Error cargando estado anterior desde DB: {e}")
                return None
        else:
            try:
                fname = f"estado_{identificador}.txt"
                if os.path.exists(fname):
                    with open(fname, 'r', encoding='utf-8') as f:
                        return f.readline().strip()
                return None
            except Exception as e:
                self._log('error', identificador, f"Error cargando estado local: {e}")
                return None

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        if self.db:
            try:
                self.db.registrar_verificacion(identificador, estado, exitoso)
            except Exception as e:
                self._log('error', identificador, f"Error registrando verificación en DB: {e}")
        else:
            # guardamos en log o archivo sencillo
            self._log('info', identificador, f"Registro (simulado) - estado: {estado} - exitoso: {exitoso}")

    # ---------- Selenium inicialización ----------
    def inicializar_selenium(self):
        try:
            options = webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions")
            options.add_argument("--disable-software-rasterizer")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--window-size=1920,1080")
            # Escala si la config pide render más claro
            if self.config.get('force_device_scale', False):
                options.add_argument("--force-device-scale-factor=2")

            driver = webdriver.Chrome(options=options)
            try:
                driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            except Exception:
                pass
            wait = WebDriverWait(driver, 20)
            return driver, wait
        except Exception as e:
            self.logger.error(f"Error FATAL al inicializar Selenium: {e}")
            raise

    # ---------- CAPTCHA: captura + preprocesado + OCR ----------
    def capturar_captcha(self, driver, wait, identificador=None):
        try:
            captcha_element = wait.until(EC.visibility_of_element_located((By.ID, "imagenCaptcha")))
            # Usar canvas para obtener imagen completa (naturalWidth/naturalHeight)
            script = """
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth || img.width;
            canvas.height = img.naturalHeight || img.height;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png');
            """
            image_base64 = driver.execute_script(script, captcha_element)
            image_bytes = base64.b64decode(image_base64.split(',')[1])
            captcha_path = os.path.join(tempfile.gettempdir(), f"captcha_{int(time.time()*1000)}.png")
            with open(captcha_path, 'wb') as f:
                f.write(image_bytes)
            self._log('info', identificador, f"Imagen CAPTCHA capturada: {captcha_path}")
            return captcha_path
        except Exception as e:
            self._log('error', identificador, f"Error al capturar CAPTCHA: {e}")
            return None

    def preprocesar_imagen(self, image_path, identificador=None):
        """Preprocesado estándar (x4, contraste 4, umbral 150)"""
        try:
            image = Image.open(image_path)
            image = image.resize((image.width*4, image.height*4), Image.LANCZOS)
            image = image.convert('L')
            image = ImageEnhance.Contrast(image).enhance(4.0)
            image = image.point(lambda p: 255 if p > 150 else 0)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            out = image_path.replace('.png', '_proc.png')
            image.save(out)
            self._log('debug', identificador, f"Imagen preprocesada guardada: {out}")
            return out
        except Exception as e:
            self._log('error', identificador, f"Error preprocesando imagen: {e}")
            return image_path

    def resolver_captcha(self, image_path, identificador=None):
        """
        Intenta OCR sobre la imagen preprocesada.
        Devuelve el mejor resultado que cumpla la longitud mínima.
        """
        try:
            # Preprocesar imagen
            processed_path = self.preprocesar_imagen(image_path, identificador)
            
            best = ""
            best_score = -1
            
            try:
                img = Image.open(processed_path)
                custom_config = f'--oem {self.OCR_OEM} --psm {self.OCR_PSM} -c tessedit_char_whitelist={self.OCR_WHITELIST}'
                raw = pytesseract.image_to_string(img, config=custom_config).strip()
                cleaned = ''.join(ch for ch in raw if ch.isdigit())
                score = len(cleaned)
                self._log('debug', identificador, f"OCR raw: '{raw}' -> cleaned: '{cleaned}' (score {score})")
                
                if score > best_score:
                    best = cleaned
                    best_score = score
            except Exception as e:
                self._log('warning', identificador, f"OCR fallo en imagen procesada: {e}")

            # Validación final
            if best and len(best) >= self.OCR_MIN_LEN:
                # Si viene más largo (ej: >6), truncar a la longitud mínima
                texto_final = best[:self.OCR_MIN_LEN]
                self._log('info', identificador, f"OCR validado: '{texto_final}' (original {best})")
                return texto_final
            else:
                self._log('warning', identificador, f"OCR no válido o corto: '{best}'")
                return ""
        except Exception as e:
            self._log('error', identificador, f"Error resolviendo CAPTCHA: {e}")
            return ""

    # ---------- Interacción formulario / extracción ----------
    def esperar_opcion_visado(self, driver, wait):
        wait.until(EC.presence_of_element_located((By.XPATH, "//select[@id='infServicio']/option[@value='VISADO']")))

    def interactuar_con_formulario(self, driver, wait, identificador, ano_nacimiento, captcha_texto):
        try:
            tipo_tramite_select_element = wait.until(EC.element_to_be_clickable((By.ID, "infServicio")))
            self.esperar_opcion_visado(driver, wait)
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

            # Pequeña pausa y click robusto
            time.sleep(random.uniform(0.3, 1.0))
            try:
                driver.execute_script("arguments[0].click();", submit_button)
            except Exception:
                submit_button.click()
            self._log('info', identificador, "Formulario enviado.")
            return True
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
            self._log('error', identificador, f"Error interactuando con formulario: {e}")
            return False
        except Exception as e:
            self._log('error', identificador, f"Error inesperado interactuando: {e}")
            return False

    def extraer_estado(self, driver, wait, identificador=None):
        try:
            # Esperar contenido (robusto)
            wait.until(EC.presence_of_element_located((By.ID, "CajaGenerica")))
            wait.until(lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip() != "")
            wait.until(lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip() != "")

            titulo = driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip().upper()
            desc = driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip()
            estado = f"{titulo} - {desc}"
            self._log('info', identificador, f"Estado extraído: {estado}")
            return estado
        except Exception as e:
            # Comprobar mensaje de error de captcha
            try:
                err_el = driver.find_element(By.ID, "CompararCaptcha")
                if err_el and "no concuerdan con la imagen" in err_el.text.lower():
                    self._log('warning', identificador, "Servidor indica CAPTCHA incorrecto.")
                    return None
            except Exception:
                pass
            self._log('error', identificador, f"No se pudo extraer estado: {e}")
            return None

    # ---------- Flujo por cuenta ----------
    def consultar_estado_para_cuenta(self, driver, wait, identificador, ano_nacimiento):
        intentos = 0
        while intentos < self.MAX_REINTENTOS:
            intentos += 1
            self._log('info', identificador, f"Intento {intentos}/{self.MAX_REINTENTOS}")
            try:
                driver.get("https://sutramiteconsular.maec.es/")
                time.sleep(random.uniform(1.5, 3.5))
                captcha_path = self.capturar_captcha(driver, wait, identificador)
                if not captcha_path:
                    self._log('warning', identificador, "No se capturó captcha, reintentando.")
                    time.sleep(random.uniform(2,5))
                    continue

                captcha_text = self.resolver_captcha(captcha_path, identificador)

                # limpiar ficheros temporales (dejamos los procesados por seguridad)
                try:
                    if os.path.exists(captcha_path):
                        os.remove(captcha_path)
                except:
                    pass

                if not captcha_text:
                    self.registrar_verificacion(identificador, "CAPTCHA_FAIL", exitoso=False)
                    time.sleep(random.uniform(2,5))
                    continue

                if not self.interactuar_con_formulario(driver, wait, identificador, ano_nacimiento, captcha_text):
                    self.registrar_verificacion(identificador, "INTERACT_FAIL", exitoso=False)
                    time.sleep(random.uniform(2,5))
                    continue

                estado = self.extraer_estado(driver, wait, identificador)
                if estado is not None:
                    self.registrar_verificacion(identificador, estado, exitoso=True)
                    return estado
                else:
                    # Probablemente captcha fallido en servidor
                    self.registrar_verificacion(identificador, "CAPTCHA_REJECTED", exitoso=False)
                    time.sleep(random.uniform(3,6))
                    continue

            except WebDriverException as e:
                self._log('critical', identificador, f"WebDriverException crítica: {e}")
                self.registrar_verificacion(identificador, "ERROR_DRIVER", exitoso=False)
                return None
            except Exception as e:
                self._log('error', identificador, f"Error inesperado en intento: {e}")
                self.registrar_verificacion(identificador, "ERROR_INTERNO", exitoso=False)
                time.sleep(random.uniform(2,5))
                continue

        self._log('error', identificador, "Falló tras todos los reintentos de CAPTCHA.")
        return None

    # ---------- Worker y ejecución paralela ----------
    def worker_cuenta(self, cuenta):
        identificador = cuenta.get('identificador')
        ano_nacimiento = cuenta.get('año_nacimiento')
        driver = None
        wait = None
        try:
            self._log('info', identificador, "Iniciando driver para cuenta...")
            driver, wait = self.inicializar_selenium()
            estado_anterior = self.cargar_estado_anterior(identificador)
            estado_actual = self.consultar_estado_para_cuenta(driver, wait, identificador, ano_nacimiento)
            if estado_actual:
                if estado_anterior is None or estado_actual != estado_anterior:
                    self.guardar_estado(identificador, estado_actual)
                    asunto = f"🚨 Cambio de estado: {self._display_name(identificador)}"
                    cuerpo = f"Se detectó cambio para {self._display_name(identificador)}\nNuevo estado: {estado_actual}\nFecha: {time.strftime('%Y-%m-%d %H:%M:%S')}"
                    self.enviar_notificacion(asunto, cuerpo, identificador)
                else:
                    self._log('info', identificador, f"Sin cambios: {estado_actual}")
            else:
                self._log('warning', identificador, "No se obtuvo estado válido tras intentos.")
        except Exception as e:
            self._log('error', identificador, f"Error worker_cuenta: {e}")
        finally:
            try:
                if driver:
                    driver.quit()
                    self._log('info', identificador, "Driver cerrado.")
            except Exception as e:
                self._log('warning', identificador, f"Error cerrando driver: {e}")

    def ejecutar_monitoreo(self):
        self.logger.info(f"Iniciando ciclo de monitoreo (concurrencia {self.MAX_CONCURRENCIA}).")
        # map no vuelve hasta que estén todos
        self.executor.map(self.worker_cuenta, self.cuentas)
        self.logger.info("Ciclo de monitoreo completado.")

    # ---------- Notificaciones (Resend) ----------
    def _get_email_destino(self, identificador):
        for c in self.cuentas:
            if c.get('identificador') == identificador:
                return c.get('email_notif') or self.config.get('notificaciones', {}).get('email_destino')
        return self.config.get('notificaciones', {}).get('email_destino')

    def enviar_notificacion(self, asunto, cuerpo, identificador_destino, es_html=False):
        email_dest = self._get_email_destino(identificador_destino) if identificador_destino != "__RESUMEN__" else self.config.get('notificaciones', {}).get('email_destino')
        if not email_dest:
            self._log('error', identificador_destino, "No hay email destino configurado, notificación omitida.")
            return

        resend_api_key = os.environ.get('RESEND_API_KEY')
        if not resend_api_key or not HAS_REQUESTS:
            # fallback: log / archivo
            if identificador_destino == "__RESUMEN__":
                self.logger.info(f"(SIMULADO RESUMEN) {asunto}")
                try:
                    with open("resumen_simulado.html", "w", encoding="utf-8") as f:
                        f.write(cuerpo if es_html else f"<pre>{cuerpo}</pre>")
                except:
                    pass
            else:
                self._log('info', identificador_destino, f"(SIMULADO) {asunto}")
            return

        # Enviar via Resend
        try:
            headers = {"Authorization": f"Bearer {resend_api_key}", "Content-Type": "application/json"}
            payload = {
                "from": "BOT Visado <onboarding@resend.dev>",
                "to": [email_dest],
                "subject": asunto,
                "html": cuerpo if es_html else f"<pre style='font-family: Arial, sans-serif; white-space: pre-wrap;'>{cuerpo}</pre>"
            }
            resp = requests.post("https://api.resend.com/emails", headers=headers, json=payload, timeout=30)
            if resp.status_code == 200:
                if identificador_destino == "__RESUMEN__":
                    self.logger.info(f"✅ (RESUMEN) Email enviado vía Resend a {email_dest}")
                else:
                    self._log('info', identificador_destino, f"✅ Email enviado vía Resend a {email_dest}")
            else:
                self._log('error', identificador_destino, f"Resend API error: {resp.status_code} - {resp.text}")
        except Exception as e:
            self._log('error', identificador_destino, f"Error enviando email via Resend: {e}")

    # ---------- Resumen 12h (HTML oscuro, similar a bot_visado.py) ----------
    def generar_html_resumen_12h(self, resumen_global):
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
        .footer { margin-top: 14px; color: #93b0d6; font-size: 12px; }
        """
        html = f"""<html><head><meta charset="utf-8"><style>{css}</style></head><body>
        <div class="card">
          <h1>📊 Resumen de Monitoreo - Últimas {self.resumen_interval_hours} horas</h1>
          <div class="meta">{resumen_global.get('resumen_texto','')}</div>
          <table role="presentation" cellspacing="0" cellpadding="0">
            <thead><tr><th>Hora</th><th>Nombre</th><th>Estado</th><th>Resultado</th></tr></thead>
            <tbody>{resumen_global.get('tabla_rows','')}</tbody>
          </table>
          <div class="footer">Enviado por <strong>BOT Visado</strong> • {time.strftime('%Y-%m-%d %H:%M:%S')}</div>
        </div></body></html>"""
        return html

    def enviar_resumen_12h(self):
        try:
            now = datetime.now()
            cutoff = now - timedelta(hours=self.resumen_interval_hours)
            cutoff_str = cutoff.strftime('%Y-%m-%d %H:%M:%S')

            tabla_rows = []
            total_mon = 0
            total_err = 0
            cuentas_incl = 0

            for c in self.cuentas:
                ident = c.get('identificador')
                nombre = c.get('nombre', ident)
                historial = []
                if self.db:
                    try:
                        historial = self.db.cargar_historial(ident)
                    except Exception as e:
                        self._log('warning', ident, f"Error cargando historial DB: {e}")
                else:
                    # Si no hay DB intentamos abrir archivo histórico sencillo (si se guarda)
                    historial = []

                recientes = []
                for e in historial:
                    fh = e.get('fecha_hora')
                    if not fh:
                        continue
                    try:
                        if fh >= cutoff_str:
                            recientes.append(e)
                    except Exception:
                        continue

                if not recientes:
                    continue
                cuentas_incl += 1
                for r in recientes:
                    hora = r.get('fecha_hora', '')
                    estado = (r.get('estado') or "").replace('\n',' ').strip()
                    exitoso = r.get('exitoso', False)
                    resultado_html = "<span class='ok'>OK</span>" if exitoso else "<span class='err'>ERROR</span>"
                    if not exitoso:
                        total_err += 1
                    tabla_rows.append(f"<tr><td>{hora}</td><td>{nombre}</td><td>{estado}</td><td>{resultado_html}</td></tr>")
                    total_mon += 1

            resumen_texto = f"Resumen desde {cutoff_str} hasta {now.strftime('%Y-%m-%d %H:%M:%S')}. Cuentas con actividad: {cuentas_incl}"
            resumen_global = {
                "resumen_texto": resumen_texto,
                "tabla_rows": "\n".join(tabla_rows) if tabla_rows else "<tr><td colspan='4' style='color:#9fb3d6;padding:12px;'>No se registraron monitoreos en el periodo.</td></tr>",
                "totals": {"cuentas": cuentas_incl, "monitoreos": total_mon, "errores": total_err}
            }
            html = self.generar_html_resumen_12h(resumen_global)
            asunto = f"Resumen de Monitoreo (Últimas {self.resumen_interval_hours}h) - {time.strftime('%Y-%m-%d %H:%M:%S')}"
            self.enviar_notificacion(asunto, html, identificador_destino="__RESUMEN__", es_html=True)
            self.logger.info("Resumen 12h generado/enviado.")
        except Exception as e:
            self.logger.error(f"Error generando resumen 12h: {e}")

    # ---------- Inicio / cierre ----------
    def iniciar(self):
        """
        Ejecuta un ciclo inmediato, luego entra en bucle programado.
        """
        self.logger.info(f"Monitoreo {len(self.cuentas)} cuentas cada {self.monitoreo_interval_hours} horas. Resumen cada {self.resumen_interval_hours} horas.")
        
        # Programar tareas
        schedule.every(self.monitoreo_interval_hours).hours.do(self.ejecutar_monitoreo)
        schedule.every(self.resumen_interval_hours).hours.do(self.enviar_resumen_12h)
        
        # Ejecución inmediata
        self.logger.info("Ejecutando primer monitoreo inmediato...")
        self.ejecutar_monitoreo()
        
        self.logger.info("Enviando primer resumen inmediato...")
        self.enviar_resumen_12h()
        
        self.logger.info(f"Programando monitoreo cada {self.monitoreo_interval_hours} horas...")
        self.logger.info(f"Programando resumen cada {self.resumen_interval_hours} horas...")
        
        # Bucle principal
        while True:
            try:
                schedule.run_pending()
                time.sleep(60)  # Verificar cada minuto
            except KeyboardInterrupt:
                self.logger.info("Interrupción por teclado.")
                break
            except Exception as e:
                self.logger.error(f"Error en bucle principal: {e}")
                time.sleep(60)

    def cerrar(self):
        try:
            self.executor.shutdown(wait=True)
        except:
            pass
        if self.db:
            try:
                self.db.close()
            except:
                pass
        self.logger.info("Bot finalizado. Recursos liberados.")

# ------------------ Ejecución principal ------------------
if __name__ == "__main__":
    bot = BotVisado()
    try:
        bot.iniciar()
    except KeyboardInterrupt:
        bot.cerrar()
    except Exception as e:
        bot.logger.error(f"Error fatal: {e}")
        bot.cerrar()






