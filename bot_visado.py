# bot_visado_corregido.py

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException, StaleElementReferenceException
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import time
import schedule
import logging
import yaml
import os
import tempfile
import base64
from concurrent.futures import ThreadPoolExecutor # <- CAMBIO: Usamos ThreadPoolExecutor
from datetime import datetime, timedelta
import requests
import json
import random # Para pausas más humanas

# **IMPORTANTE**: Asumo que tu archivo 'database.py' y su clase 'DatabaseManager' están disponibles.
# from database import DatabaseManager 
# Si 'database.py' no existe o no se incluye, este código fallará.

# --- CLASE PRINCIPAL ---

class BotVisado:
    # Máximo de navegadores a ejecutar en paralelo
    MAX_CONCURRENCIA = 4 # <- CRUCIAL para Railway. Ajusta según los límites de RAM/CPU.
    
    def __init__(self, config_path="config.yaml"):
        self.config = self.cargar_config(config_path)
        self.setup_logging()
        
        # Inicializar base de datos PostgreSQL
        # self.db = DatabaseManager() # <-- Descomentar si usas la base de datos
        self.db = None # Si no está disponible, mantener None
        
        # Cargar lista de cuentas
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración.")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")
        
        # Atributo para el pool de hilos
        self.executor = ThreadPoolExecutor(max_workers=self.MAX_CONCURRENCIA)

    def cargar_config(self, path):
        with open(path, 'r', encoding='utf-8') as f:
            return yaml.safe_load(f)

    def setup_logging(self):
        # ... (Mantener tu configuración de logging) ...
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler('bot.log', encoding='utf-8'),
                logging.StreamHandler()
            ]
        )
        self.logger = logging.getLogger(__name__)


    # --- Helpers para logs y DB (se mantiene tu lógica) ---
    def _display_name(self, identificador):
        try:
            for cuenta in self.cuentas:
                if cuenta.get('identificador') == identificador:
                    nombre = cuenta.get('nombre')
                    if nombre:
                        return nombre
        except Exception:
            pass
        return identificador

    def _log(self, nivel, identificador, mensaje):
        display = self._display_name(identificador)
        prefix = f"({display}) " if display else ""
        getattr(self.logger, nivel)(f"{prefix}{mensaje}")

    def cargar_estado_anterior(self, identificador):
        # if self.db: return self.db.cargar_estado_anterior(identificador)
        return None
    
    def guardar_estado(self, identificador, estado):
        # if self.db: return self.db.guardar_estado(identificador, estado)
        return True

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        # if self.db: return self.db.registrar_verificacion(identificador, estado, exitoso)
        return True

    def cargar_historial(self, identificador):
        # if self.db: return self.db.cargar_historial(identificador)
        return []

    # --- INICIALIZACIÓN DE SELENIUM (CORREGIDA para Railway) ---
    def inicializar_selenium(self):
        """Inicializa y devuelve un nuevo driver y wait, con opciones optimizadas para Railway."""
        try:
            options = webdriver.ChromeOptions()
            # Opciones esenciales para entornos headless (Railway) y evitar el OOM Kill
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-gpu")
            options.add_argument("--disable-extensions") # Ahorrar recursos
            options.add_argument("--disable-software-rasterizer") # Ahorrar recursos
            # Opciones anti-detección
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            # Tamaño de ventana fijo para captura de CAPTCHA
            options.add_argument("--window-size=1920,1080")
            # Eliminar el scale factor que puede causar problemas
            # options.add_argument("--force-device-scale-factor=2") # Eliminado para evitar renderizado doble
            
            driver = webdriver.Chrome(options=options)
            driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            wait = WebDriverWait(driver, 20)  # Aumentado a 20s para mayor estabilidad en Railway
            return driver, wait
        except Exception as e:
            self.logger.error(f"❌ Error FATAL al inicializar Selenium. Verificar ChromeDriver/entorno: {str(e)}")
            raise

    # --- CAPTCHA / OCR / Imagen (MEJORADO) ---
    # `capturar_captcha` se mantiene bien, usa JS/Base64.

    def capturar_captcha(self, driver, wait, identificador=None):
        """Captura una imagen del CAPTCHA usando JavaScript (base64) y la guarda en temp."""
        try:
            captcha_element = wait.until(
                EC.visibility_of_element_located((By.ID, "imagenCaptcha"))
            )
            # Código JS se mantiene (es la mejor forma de captura de imagen)
            script = """
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.naturalWidth; // Usar el tamaño real
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
            self._log('info', identificador, f"Imagen CAPTCHA capturada y guardada.")
            return captcha_path
        except Exception as e:
            self._log('error', identificador, f"Error al capturar el CAPTCHA: {e}")
            return None

    def preprocesar_captcha(self, image_path, identificador=None):
        """Preprocesa la imagen CAPTCHA para mejorar OCR (dígitos). OPTIMIZADO V2"""
        try:
            image = Image.open(image_path)
            
            # 1. Escalado (Aumento de resolución)
            new_size = (image.width * 5, image.height * 5) # Aumentar a x5 (más agresivo)
            image = image.resize(new_size, Image.LANCZOS)
            
            # 2. Conversión a escala de grises
            image = image.convert('L')
            
            # 3. Aumento de Contraste más fuerte
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(5.0) # Más contraste

            # 4. Binarización (Umbral para aislar los dígitos)
            # El valor 180 es una buena aproximación para fondos claros y dígitos oscuros.
            image = image.point(lambda p: p > 180 and 255) 
            
            # 5. Filtro de Mediana (Eliminar ruido 'salt and pepper' sin desenfoque)
            image = image.filter(ImageFilter.MedianFilter(size=3))
            
            # 6. Aislamiento de dígitos: Usar un Umbral inverso más bajo puede ayudar
            # image = image.point(lambda p: 255 if p > 180 else 0) # Binarización limpia

            processed_path = image_path.replace('.png', '_processed.png')
            image.save(processed_path)
            self._log('info', identificador, f"Imagen CAPTCHA preprocesada.")
            return processed_path
        except Exception as e:
            self._log('error', identificador, f"Error al preprocesar CAPTCHA: {e}")
            return image_path

    def resolver_captcha(self, image_path, identificador=None):
        """Resuelve el CAPTCHA con Tesseract usando configuraciones optimizadas."""
        try:
            image = Image.open(image_path)
            # psm 8: Assume a single word (mejor para dígitos).
            # whitelist: Limita a solo 0-9.
            custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(image, config=custom_config).strip()
            texto_limpio = ''.join(c for c in texto if c.isdigit())
            self._log('debug', identificador, f"Texto OCR (original): '{texto}'")
            self._log('info', identificador, f"Texto OCR (limpio): '{texto_limpio}'")
            if len(texto_limpio) == 6:  # Validar 6 dígitos
                return texto_limpio
            else:
                self._log('warning', identificador, f"OCR fallido. Longitud {len(texto_limpio)} != 6.")
                return ""
        except Exception as e:
            self._log('error', identificador, f"Error al resolver CAPTCHA con OCR: {e}")
            return ""

    # --- Interacción y extracción ---
    # `interactuar_con_formulario` y `extraer_estado` se mantienen, pero se añade manejo de StaleElementReferenceException
    
    def wait_for_option_visado(self, driver, wait):
        # Aumentar robustez en el wait
        wait.until(
            EC.presence_of_element_located((By.XPATH, "//select[@id='infServicio']/option[@value='VISADO']"))
        )

    def interactuar_con_formulario(self, driver, wait, identificador, ano_nacimiento, captcha_texto):
        try:
            # Esperar a que el select esté visible y seleccionable
            tipo_tramite_select_element = wait.until(
                EC.element_to_be_clickable((By.ID, "infServicio"))
            )
            self._log('info', identificador, "Select 'infServicio' presente.")
            
            # Asegurar que la opción VISADO esté cargada (tu helper)
            self.wait_for_option_visado(driver, wait)
            
            from selenium.webdriver.support.ui import Select
            select = Select(tipo_tramite_select_element)
            select.select_by_value("VISADO")

            # Esperar a que todos los campos de entrada estén presentes
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
            time.sleep(random.uniform(0.5, 1.5)) # Pausa aleatoria
            
            # Usar JS para el click en caso de que Selenium falle (más robusto)
            driver.execute_script("arguments[0].click();", submit_button)
            self._log('info', identificador, f"Formulario enviado para {identificador}.")
            return True
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
            self._log('error', identificador, f"Error al interactuar con el formulario: {e}. Reintentando.")
            return False
        except Exception as e:
            self._log('error', identificador, f"Error inesperado interactuando con formulario: {e}")
            return False
    
    def extraer_estado(self, driver, wait, identificador=None):
        try:
            # Esperar a que la caja contenedora y los títulos estén presentes y con texto
            wait.until(EC.presence_of_element_located((By.ID, "CajaGenerica")))
            wait.until(
                lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip() != ""
            )
            wait.until(
                lambda drv: drv.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip() != ""
            )
            
            # Extraer y limpiar
            titulo_estado = driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip().upper()
            desc_estado = driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip()
            estado_completo = f"{titulo_estado} - {desc_estado}"
            self._log('info', identificador, f"Estado extraído: {estado_completo}")
            return estado_completo
        except (TimeoutException, NoSuchElementException, StaleElementReferenceException) as e:
            # Intentar detectar el error de CAPTCHA si la extracción del estado falla
            try:
                error_captcha_element = driver.find_element(By.ID, "CompararCaptcha")
                if error_captcha_element.is_displayed():
                    error_text = error_captcha_element.text
                    self._log('warning', identificador, f"❌ Mensaje de error de CAPTCHA del servidor: {error_text}")
                    return None
            except NoSuchElementException:
                self._log('info', identificador, "No se encontró mensaje de error de CAPTCHA específico.")
            
            self._log('error', identificador, f"Error al extraer el estado (Timeout/Elemento no encontrado): {e}")
            return None
        except Exception as e:
            self._log('error', identificador, f"Error inesperado al extraer estado: {e}")
            return None


    # --- CONSULTA POR CUENTA (FLUJO MEJORADO) ---
    def consultar_estado_para_cuenta(self, driver, wait, identificador, ano_nacimiento):
        """Intenta múltiples reintentos del captcha y la consulta. Devuelve estado o None."""
        max_reintentos_captcha = 15 # Aumentado a 15, ya que el OCR es inestable
        intentos = 0
        
        while intentos < max_reintentos_captcha:
            self._log('info', identificador, f"Intento {intentos + 1} de {max_reintentos_captcha} de CAPTCHA.")
            
            # 1. Navegar y esperar la carga de la página
            try:
                driver.get("https://sutramiteconsular.maec.es/") 
                time.sleep(random.uniform(2.5, 4.0)) # Pausa más larga y aleatoria
            except WebDriverException as e:
                self._log('error', identificador, f"❌ FALLO CRÍTICO DE NAVEGACIÓN (WebDriver): {e}")
                self.registrar_verificacion(identificador, "ERROR_DRIVER_NAV", exitoso=False)
                return None # No reintentar si la navegación falla por driver
                
            captcha_path = None
            processed_path = None
            
            try:
                # 2. Captura y resolución del CAPTCHA
                captcha_path = self.capturar_captcha(driver, wait, identificador)
                if not captcha_path:
                    raise Exception("No se pudo capturar el CAPTCHA.")

                processed_path = self.preprocesar_captcha(captcha_path, identificador)
                captcha_texto = self.resolver_captcha(processed_path, identificador)

                if not captcha_texto:
                    raise Exception("OCR no pudo resolver el CAPTCHA.")

                # 3. Interacción y envío del formulario
                if not self.interactuar_con_formulario(driver, wait, identificador, ano_nacimiento, captcha_texto):
                    # Si falla la interacción (Timeout, StaleElement, etc.)
                    raise Exception("Fallo en la interacción con el formulario.")

                # 4. Extracción del estado (si es exitoso, rompe el loop)
                estado = self.extraer_estado(driver, wait, identificador)

                if estado is not None:
                    self.registrar_verificacion(identificador, estado, exitoso=True)
                    return estado
                else:
                    # El servidor devolvió un error (probablemente CAPTCHA incorrecto)
                    self.registrar_verificacion(identificador, "CAPTCHA_FAIL", exitoso=False)
                    intentos += 1
                    time.sleep(random.uniform(4, 7)) # Pausa más larga tras fallo del servidor
                    continue
                    
            except WebDriverException as e:
                self._log('error', identificador, f"❌ WebDriverException en la consulta: {e}")
                self.registrar_verificacion(identificador, "ERROR_DRIVER_OP", exitoso=False)
                # Al producirse una WebDriverException, el driver está corrupto.
                # Se debe salir del worker y dejar que el `finally` lo cierre y lo reintente en el próximo ciclo.
                return None 
            except Exception as e:
                self._log('warning', identificador, f"Fallo en el intento {intentos + 1}: {e}")
                self.registrar_verificacion(identificador, "ERROR_INTERNO", exitoso=False)
                intentos += 1
                time.sleep(random.uniform(2, 4))
                continue
            finally:
                # Eliminar archivos temporales después de cada intento
                for path in [captcha_path, processed_path]:
                    try:
                        if path and os.path.exists(path):
                            os.remove(path)
                    except Exception:
                        pass
        
        # Si sale del loop por max_reintentos
        self._log('error', identificador, "Consulta fallida tras todos los reintentos de CAPTCHA.")
        return None

    # --- Worker por cuenta (usado por cada hilo) ---
    def worker_cuenta(self, cuenta):
        identificador = cuenta.get('identificador')
        ano_nacimiento = cuenta.get('año_nacimiento')
        driver = None
        wait = None
        try:
            self._log('info', identificador, "Iniciando driver...")
            # Inicializa y lanza la WebDriverException si es un error fatal
            driver, wait = self.inicializar_selenium() 
            
            estado_anterior = self.cargar_estado_anterior(identificador)
            estado_actual = self.consultar_estado_para_cuenta(driver, wait, identificador, ano_nacimiento)
            
            # Lógica de notificación se mantiene (omitiendo su código aquí por brevedad)
            if estado_actual is not None:
                hay_cambio = estado_actual != estado_anterior
                es_primera_vez = estado_anterior is None

                if hay_cambio or es_primera_vez:
                    self.guardar_estado(identificador, estado_actual)
                    display_name = self._display_name(identificador)
                    asunto = f"🚨 Cambio de Estado para {display_name}: {estado_actual}"
                    cuerpo = f"Nuevo Estado: {estado_actual}"
                    # self.enviar_notificacion(asunto, cuerpo, identificador) # <- Descomentar
                else:
                    self._log('info', identificador, f"Sin cambios: {estado_actual}")
            else:
                self._log('error', identificador, "No se obtuvo estado válido tras reintentos.")
        
        # --- BLOQUE CRÍTICO: GESTIÓN DE EXCEPCIONES Y CIERRE ---
        except WebDriverException as e:
             # Captura si el driver falla al inicializar o en un punto no manejado
            self._log('critical', identificador, f"❌ Falla Crítica del WebDriver. La instancia debe ser eliminada: {e}")
            self.registrar_verificacion(identificador, "ERROR_DRIVER_FATAL", exitoso=False)
        except Exception as e:
            self._log('error', identificador, f"Error en worker_cuenta: {e}")
        finally:
            # CIERRE ABSOLUTO DEL DRIVER para liberar recursos en Railway
            try:
                if driver:
                    driver.quit() # Usar quit() para cerrar navegador y driver
                    self._log('info', identificador, "Driver cerrado (quit()).")
            except Exception as e:
                self._log('warning', identificador, f"Error cerrando driver (es posible que ya estuviera colgado): {e}")

    # --- Ejecución del monitoreo (USANDO ThreadPoolExecutor) ---
    def ejecutar_monitoreo(self):
        """Usa ThreadPoolExecutor para limitar la concurrencia."""
        self.logger.info(f"Iniciando ciclo de monitoreo. Máx. Concurrencia: {self.MAX_CONCURRENCIA}.")
        
        # El executor ya se inicializó en __init__
        self.executor.map(self.worker_cuenta, self.cuentas) 

        self.logger.info("Ciclo de monitoreo para todas las cuentas completado.")

    def iniciar(self):
        # ... (Tu lógica de schedule se mantiene) ...
        intervalo_horas = self.config.get('intervalo_horas', 0.5)
        intervalo_segundos = intervalo_horas * 3600
        schedule.every(intervalo_segundos).seconds.do(self.ejecutar_monitoreo)
        schedule.every(12).hours.do(self.enviar_resumen_12h)
        self.logger.info(f"Monitoreo para {len(self.cuentas)} cuentas cada {intervalo_segundos/60:.1f} minutos. Resumen cada 12 horas.")
        self.ejecutar_monitoreo()
        while True:
            schedule.run_pending()
            time.sleep(60)

    def cerrar(self):
        # Cerrar el executor y la conexión a la base de datos
        self.executor.shutdown(wait=True)
        # if hasattr(self, 'db') and self.db:
        #     self.db.close()
        self.logger.info("Bot finalizado. Conexiones cerradas.")

# --- Ejecución Principal (se mantiene) ---
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

