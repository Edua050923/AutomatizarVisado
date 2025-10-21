# bot_visado_final.py
# Optimizado para Railway (Entorno Headless)
# Control de Concurrencia con ThreadPoolExecutor
# OCR mejorado para el CAPTCHA
# Resumen por email cada hora

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException
from selenium.webdriver.support.ui import Select
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import time
import schedule
import logging
import yaml
import os
import tempfile
import base64
from concurrent.futures import ThreadPoolExecutor # Para limitar la concurrencia
from datetime import datetime, timedelta
import random # Para pausas más humanas

# **NOTA:** La importación de 'database' y la lógica de notificaciones
# se mantienen como 'placeholders' (comentados o simplificados) ya que
# su implementación completa no fue provista, pero son esenciales
# para el funcionamiento de la persistencia y los emails.
# from database import DatabaseManager 

# --- CLASE PRINCIPAL ---

class BotVisado:
    # Máximo de navegadores a ejecutar en paralelo (ajustar según los límites de RAM/CPU de Railway)
    MAX_CONCURRENCIA = 4 
    
    def __init__(self, config_path="config.yaml"):
        self.config = self.cargar_config(config_path)
        self.setup_logging()
        
        # Inicializar base de datos (placeholders)
        # self.db = DatabaseManager() 
        self.db = None
        
        # Cargar lista de cuentas
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración.")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")
        
        # Inicializar el pool de hilos para controlar la concurrencia
        self.executor = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENCIA)

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

    # --- Helpers de Logs y DB (simplificados) ---
    def _display_name(self, identificador):
        try:
            for cuenta in self.cuentas:
                if cuenta.get('identificador') == identificador:
                    return cuenta.get('nombre')
        except Exception:
            pass
        return identificador

    def _log(self, nivel, identificador, mensaje):
        display = self._display_name(identificador)
        prefix = f"({display}) " if display else ""
        getattr(self.logger, nivel)(f"{prefix}{mensaje}")

    # Estos métodos deben interactuar con tu 'DatabaseManager' real
    def cargar_estado_anterior(self, identificador):
        # if self.db: return self.db.cargar_estado_anterior(identificador)
        return None
    
    def guardar_estado(self, identificador, estado):
        # if self.db: return self.db.guardar_estado(identificador, estado)
        return True

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        # if self.db: return self.db.registrar_verificacion(identificador, estado, exitoso)
        return True

    def enviar_resumen(self):
        """Función para enviar el resumen (ahora se ejecuta cada hora)."""
        self.logger.info("📧 Enviando resumen de estados por email...")
        # Lógica real de Resend/SMTP/Email debe ir aquí.
        # Por ejemplo:
        # estados = self.db.obtener_resumen_estados()
        # self.enviar_email_resumen(estados) 
        self.logger.info("✅ Resumen enviado con éxito (o lógica simulada).")

    # --- INICIALIZACIÓN DE SELENIUM (CRUCIAL PARA RAILWAY) ---
    def inicializar_selenium(self):
        """Inicializa driver con opciones optimizadas para entornos headless."""
        try:
            options = webdriver.ChromeOptions()
            # Opciones esenciales para estabilidad y bajo consumo de recursos
            options.add_argument("--headless=new") # Modo headless moderno
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions") 
            options.add_argument("--disable-software-rasterizer")
            
            # Opciones anti-detección (se mantienen)
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            options.add_argument("--window-size=1920,1080")
            
            driver = webdriver.Chrome(options=options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            wait = WebDriverWait(driver, 20)  # 20 segundos para mayor estabilidad
            return driver, wait
        except Exception as e:
            self.logger.error(f"❌ Error FATAL al inicializar Selenium: {str(e)}")
            raise

    # --- CAPTCHA / OCR (MEJORADO) ---

    def capturar_captcha(self, driver, wait, identificador=None):
        """Captura el CAPTCHA con JS."""
        try:
            captcha_element = wait.until(
                EC.visibility_of_element_located((By.ID, "imagenCaptcha"))
            )
            script = """
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth;
            canvas.height = img.naturalHeight;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png');
            """
            image_base64 = driver.execute_script(script, captcha_element)
            image_bytes = base64.b64decode(image_base64.split(',')[1])
            captcha_path = os.path.join(tempfile.gettempdir(), f"captcha_{int(time.time()*1000)}.png")
            with open(captcha_path, 'wb') as f:
                f.write(image_bytes)
            self._log('info', identificador, "Imagen CAPTCHA capturada.")
            return captcha_path
        except Exception as e:
            self._log('error', identificador, f"Error al capturar el CAPTCHA: {e}")
            return None

    def preprocesar_captcha(self, image_path, identificador=None):
        """Preprocesa la imagen CAPTCHA para mejorar OCR (Optimizado V2)."""
        try:
            image = Image.open(image_path)
            
            # 1. Escalado agresivo (x5)
            new_size = (image.width * 5, image.height * 5)
            image = image.resize(new_size, Image.LANCZOS)
            
            # 2. Conversión a escala de grises
            image = image.convert('L')
            
            # 3. Aumento de Contraste más fuerte
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(5.0) 

            # 4. Binarización (Umbral para aislar los dígitos)
            image = image.point(lambda p: p > 180 and 255) 
            
            # 5. Filtro de Mediana (Eliminar ruido)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            
            processed_path = image_path.replace('.png', '_processed.png')
            image.save(processed_path)
            self._log('info', identificador, "Imagen CAPTCHA preprocesada.")
            return processed_path
        except Exception as e:
            self._log('error', identificador, f"Error al preprocesar CAPTCHA: {e}")
            return image_path

    def resolver_captcha(self, image_path, identificador=None):
        """Resuelve el CAPTCHA con Tesseract (PSM 8 y whitelist)."""
        try:
            image = Image.open(image_path)
            # PSM 8: Asume una única palabra. Whitelist: Solo dígitos.
            custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(image, config=custom_config).strip()
            texto_limpio = ''.join(c for c in texto if c.isdigit())
            self._log('debug', identificador, f"OCR texto limpio: '{texto_limpio}'")
            if len(texto_limpio) == 6:  # Validar 6 dígitos
                return texto_limpio
            else:
                return ""
        except Exception as e:
            self._log('error', identificador, f"Error al resolver CAPTCHA con OCR: {e}")
            return ""

    # --- Interacción ---
    
    def interactuar_con_formulario(self, driver, wait, identificador, ano_nacimiento, captcha_texto):
        try:
            # Seleccionar 'VISADO'
            tipo_tramite_select_element = wait.until(
                EC.element_to_be_clickable((By.ID, "infServicio"))
            )
            # Asegurar que la opción 'VISADO' está cargada antes de seleccionar
            wait.until(
                EC.presence_of_element_located((By.XPATH, "//select[@id='infServicio']/option[@value='VISADO']"))
            )
            select = Select(tipo_tramite_select_element)
            select.select_by_value("VISADO")

            # Rellenar campos
            identificador_input = wait.until(EC.presence_of_element_located((By.ID, "txIdentificador")))
            ano_nacimiento_input = wait.until(EC.presence_of_element_located((By.ID, "txtFechaNacimiento")))
            captcha_input = wait.until(EC.presence_of_element_located((By.ID, "imgcaptcha")))
            submit_button = wait.until(EC.element_to_be_clickable((By.ID, "imgVerSuTramite")))

            # Usar .clear() y .send_keys() para robustez
            identificador_input.clear()
            identificador_input.send_keys(identificador)
            ano_nacimiento_input.clear()
            ano_nacimiento_input.send_keys(ano_nacimiento)
            captcha_input.clear()
            captcha_input.send_keys(captcha_texto)
            
            # Simular mejor interacción: quitar el foco y pausa
            driver.execute_script("arguments[0].blur();", captcha_input)
            time.sleep(random.uniform(0.5, 1.5)) 
            
            # Click con JS para mayor robustez
            driver.execute_script("arguments[0].click();", submit_button)
            self._log('info', identificador, "Formulario enviado.")
            return True
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
            self._log('error', identificador, f"Error al interactuar con el formulario: {e}. Reintentando.")
            return False
        except Exception as e:
            self._log('error', identificador, f"Error inesperado interactuando: {e}")
            return False
    
    def extraer_estado(self, driver, wait, identificador=None):
        try:
            # Esperar a que la descripción del estado tenga contenido
            wait.until(
                lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip() != ""
            )
            
            titulo_estado = driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip().upper()
            desc_estado = driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip()
            estado_completo = f"{titulo_estado} - {desc_estado}"
            self._log('info', identificador, f"Estado extraído: {estado_completo}")
            return estado_completo
            
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException):
            # Verificar si el error es el mensaje explícito del CAPTCHA incorrecto
            try:
                error_captcha_element = driver.find_element(By.ID, "CompararCaptcha")
                if "no concuerdan con la imagen" in error_captcha_element.text:
                    self._log('warning', identificador, "❌ Servidor rechazó el CAPTCHA (OCR falló).")
                    return None
            except NoSuchElementException:
                self._log('info', identificador, "No se encontró mensaje de error de CAPTCHA.")
            
            self._log('error', identificador, "No se pudo extraer el estado.")
            return None
        except Exception as e:
            self._log('error', identificador, f"Error inesperado al extraer estado: {e}")
            return None


    # --- CONSULTA POR CUENTA (FLUJO MEJORADO) ---
    def consultar_estado_para_cuenta(self, driver, wait, identificador, ano_nacimiento):
        """Intenta múltiples reintentos del captcha y la consulta."""
        max_reintentos_captcha = 15 # Aumentado por la inestabilidad del OCR
        intentos = 0
        
        while intentos < max_reintentos_captcha:
            self._log('info', identificador, f"Intento {intentos + 1} de {max_reintentos_captcha} de CAPTCHA.")
            
            # 1. Navegar y pausa
            try:
                driver.get("https://sutramiteconsular.maec.es/") 
                time.sleep(random.uniform(2.5, 4.0)) 
            except WebDriverException as e:
                self._log('error', identificador, f"❌ FALLO CRÍTICO DE NAVEGACIÓN (WebDriver): {e}")
                self.registrar_verificacion(identificador, "ERROR_DRIVER_NAV", exitoso=False)
                return None # Sale, el driver probablemente está corrupto
                
            captcha_path = None
            processed_path = None
            
            try:
                # 2. Captura y resolución del CAPTCHA
                captcha_path = self.capturar_captcha(driver, wait, identificador)
                if not captcha_path: raise Exception("No se pudo capturar el CAPTCHA.")

                processed_path = self.preprocesar_captcha(captcha_path, identificador)
                captcha_texto = self.resolver_captcha(processed_path, identificador)

                if not captcha_texto: raise Exception("OCR no pudo resolver el CAPTCHA.")

                # 3. Interacción y envío
                if not self.interactuar_con_formulario(driver, wait, identificador, ano_nacimiento, captcha_texto):
                    # Falla de interacción (ej. Timeout), reintentar el ciclo
                    raise Exception("Fallo en la interacción con el formulario.")

                # 4. Extracción del estado
                estado = self.extraer_estado(driver, wait, identificador)

                if estado is not None:
                    # Éxito: retorna el estado y rompe el loop
                    self.registrar_verificacion(identificador, estado, exitoso=True)
                    return estado
                else:
                    # Fallo: Probablemente CAPTCHA incorrecto
                    self.registrar_verificacion(identificador, "CAPTCHA_FAIL", exitoso=False)
                    intentos += 1
                    time.sleep(random.uniform(4, 7)) # Pausa larga tras fallo del servidor
                    continue
                    
            except WebDriverException as e:
                self._log('error', identificador, f"❌ WebDriverException en la consulta: {e}")
                self.registrar_verificacion(identificador, "ERROR_DRIVER_OP", exitoso=False)
                return None # Driver corrupto, sale para cierre forzado
            except Exception as e:
                self._log('warning', identificador, f"Fallo en el intento {intentos + 1}: {e}")
                self.registrar_verificacion(identificador, "ERROR_INTERNO", exitoso=False)
                intentos += 1
                time.sleep(random.uniform(2, 4))
                continue
            finally:
                # Limpieza de archivos temporales
                for path in [captcha_path, processed_path]:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
        
        # Falla después de todos los reintentos
        self._log('error', identificador, "Consulta fallida tras todos los reintentos de CAPTCHA.")
        return None

    # --- Worker por cuenta (usado por el ThreadPoolExecutor) ---
    def worker_cuenta(self, cuenta):
        identificador = cuenta.get('identificador')
        ano_nacimiento = cuenta.get('año_nacimiento')
        driver = None
        wait = None
        try:
            self._log('info', identificador, "Iniciando driver...")
            # 1. Inicializar Driver (puede lanzar WebDriverException)
            driver, wait = self.inicializar_selenium() 
            
            # 2. Consultar estado
            estado_anterior = self.cargar_estado_anterior(identificador)
            estado_actual = self.consultar_estado_para_cuenta(driver, wait, identificador, ano_nacimiento)
            
            # 3. Lógica de estado y notificación
            if estado_actual is not None:
                if estado_actual != estado_anterior or estado_anterior is None:
                    self.guardar_estado(identificador, estado_actual)
                    self._log('warning', identificador, f"🚨 CAMBIO DE ESTADO: {estado_actual}")
                    # self.enviar_notificacion(...) 
                else:
                    self._log('info', identificador, f"Sin cambios: {estado_actual}")
            else:
                self._log('error', identificador, "No se obtuvo estado válido.")
        
        # --- BLOQUE CRÍTICO: GESTIÓN DE EXCEPCIONES Y CIERRE ---
        except WebDriverException as e:
             # Falla al inicializar o durante la operación (Driver corrupto)
            self._log('critical', identificador, f"❌ Falla Crítica del WebDriver. Cierre forzado: {e}")
            self.registrar_verificacion(identificador, "ERROR_DRIVER_FATAL", exitoso=False)
        except Exception as e:
            self._log('error', identificador, f"Error inesperado en worker_cuenta: {e}")
        finally:
            # CIERRE ABSOLUTO DEL DRIVER para liberar recursos en Railway
            try:
                if driver:
                    driver.quit() # Es vital usar quit() para cerrar procesos de Chrome
                    self._log('info', identificador, "Driver cerrado (quit()).")
            except Exception as e:
                self._log('warning', identificador, f"Error cerrando driver (posiblemente ya colgado): {e}")

    # --- Ejecución del monitoreo ---
    def ejecutar_monitoreo(self):
        """Ejecuta todos los workers limitando la concurrencia."""
        self.logger.info(f"Iniciando ciclo de monitoreo. Máx. Concurrencia: {self.MAX_CONCURRENCIA}.")
        
        # 'map' envía cada 'cuenta' a un 'worker_cuenta'
        self.executor.map(self.worker_cuenta, self.cuentas) 

        self.logger.info("Ciclo de monitoreo para todas las cuentas completado.")

    def iniciar(self):
        intervalo_horas = self.config.get('intervalo_horas', 0.5)
        intervalo_segundos = intervalo_horas * 3600
        
        # Tarea de monitoreo (e.g., cada 30 minutos)
        schedule.every(intervalo_segundos).seconds.do(self.ejecutar_monitoreo)
        
        # Tarea de resumen: CORREGIDA para enviarse CADA HORA
        schedule.every(1).hour.do(self.enviar_resumen)
        
        self.logger.info(f"Monitoreo para {len(self.cuentas)} cuentas cada {intervalo_segundos/60:.1f} minutos.")
        self.logger.info("Resumen de estado programado para enviarse CADA HORA.")
        
        self.ejecutar_monitoreo()
        
        while True:
            schedule.run_pending()
            time.sleep(60)

    def cerrar(self):
        # Cerrar el ThreadPoolExecutor
        self.executor.shutdown(wait=True)
        # if hasattr(self, 'db') and self.db:
        #     self.db.close()
        self.logger.info("Bot finalizado. Recursos cerrados.")

# --- Ejecución Principal ---
if __name__ == "__main__":
    bot = BotVisado()
    try:
        bot.iniciar()
    except KeyboardInterrupt:
        print("\nInterrupción del usuario.")
    except Exception as e:
        bot.logger.error(f"Error fatal en el loop principal: {e}")
    finally:
        bot.cerrar()



