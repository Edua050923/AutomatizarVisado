# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Instalar dependencias del sistema
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    tesseract-ocr \
    tesseract-ocr-spa \
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | apt-key add - \
    && echo "deb [arch=amd64] http://dl.google.com/linux/chrome/deb/ stable main" >> /etc/apt/sources.list.d/google.list \
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException, WebDriverException
import pytesseract
from PIL import Image, ImageEnhance, ImageFilter
import requests
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
import base64  # Import necesario para manejar la respuesta de DevTools o base64 del src

class BotVisado:
    def __init__(self, config_path="config.yaml"):
        self.config = self.cargar_config(config_path)
        self.driver = None
        self.wait = None
        self.setup_logging()
        # --- MODIFICADO: Cargar lista de cuentas ---
        self.cuentas = self.config.get('cuentas', [])
        if not self.cuentas:
            raise ValueError("No se encontraron cuentas en la configuración.")
        self.logger.info(f"Cuentas configuradas para monitoreo: {len(self.cuentas)}")
        # --- FIN MODIFICADO ---
        self.estado_actual = None # No se usa globalmente ahora, sino por cuenta
        self.estado_anterior = None # No se usa globalmente ahora, sino por cuenta
        # Los archivos de estado e historial ahora se manejan por cuenta
        
        # No se cargan estado/historial globales aquí

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

    def inicializar_selenium(self):
        """Inicializa el navegador Chrome en modo headless con alta resolución."""
        try:
            options = webdriver.ChromeOptions()
            options.add_argument("--headless=new")
            options.add_argument("--no-sandbox")
            options.add_argument("--disable-dev-shm-usage")
            options.add_argument("--disable-blink-features=AutomationControlled")
            options.add_experimental_option("excludeSwitches", ["enable-automation"])
            options.add_experimental_option('useAutomationExtension', False)
            # Opciones de resolución y escala (útiles, pero la captura ahora es más precisa)
            options.add_argument("--window-size=1920,1080")
            options.add_argument("--force-device-scale-factor=2")
            self.driver = webdriver.Chrome(options=options)
            self.driver.execute_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            self.wait = WebDriverWait(self.driver, 15)  # Aumentado el timeout general
            self.logger.info("Navegador Chrome inicializado en modo headless con alta resolución.")
        except Exception as e:
            self.logger.error(f"Error al inicializar Selenium: {str(e)}")
            raise

    def capturar_captcha(self):
        """Captura una imagen del CAPTCHA usando JavaScript para extraer la imagen como Base64."""
        try:
            # Esperar a que el elemento esté presente Y visible
            captcha_element = self.wait.until(
                EC.visibility_of_element_located((By.ID, "imagenCaptcha"))
            )

            # Usar JavaScript para extraer la imagen como Base64
            # Esto convierte la imagen en un canvas y luego a Base64
            script = """
            var img = arguments[0];
            var canvas = document.createElement('canvas');
            canvas.width = img.width;
            canvas.height = img.height;
            var ctx = canvas.getContext('2d');
            ctx.drawImage(img, 0, 0);
            return canvas.toDataURL('image/png');
            """
            image_base64 = self.driver.execute_script(script, captcha_element)

            # Decodificar la cadena Base64 a bytes
            image_bytes = base64.b64decode(image_base64.split(',')[1])

            # Guardar la imagen capturada como bytes
            captcha_path = os.path.join(tempfile.gettempdir(), "captcha.png")
            with open(captcha_path, 'wb') as f:  # 'wb' para escribir bytes
                f.write(image_bytes)

            self.logger.info(f"Imagen CAPTCHA capturada con JavaScript y guardada en: {captcha_path}")
            return captcha_path

        except Exception as e:
            self.logger.error(f"Error al capturar el CAPTCHA con JavaScript: {str(e)}")
            return None

    def preprocesar_captcha(self, image_path):
        """Preprocesa la imagen CAPTCHA para mejorar OCR, optimizado para dígitos."""
        try:
            image = Image.open(image_path)
            # Escalar la imagen 4 veces su tamaño original (más que antes)
            new_size = (image.width * 4, image.height * 4)
            image = image.resize(new_size, Image.LANCZOS)
            image = image.convert('L')  # Convertir a escala de grises
            # Aumentar el contraste
            enhancer = ImageEnhance.Contrast(image)
            image = enhancer.enhance(4)  # Aumentar el contraste
            # Aplicar un filtro de umbral más agresivo
            image = image.point(lambda p: p > 150 and 255)  # Umbral más alto
            # Aplicar un filtro de mediana para reducir ruido
            image = image.filter(ImageFilter.MedianFilter(size=3))
            processed_path = image_path.replace('.png', '_processed.png')
            image.save(processed_path)
            self.logger.info(f"Imagen CAPTCHA preprocesada guardada en: {processed_path}")
            return processed_path
        except Exception as e:
            self.logger.error(f"Error al preprocesar el CAPTCHA: {str(e)}")
            return image_path

    def resolver_captcha(self, image_path):
        try:
            image = Image.open(image_path)
            # Configuración más específica para una palabra de dígitos
            custom_config = r'--oem 3 --psm 8 -c tessedit_char_whitelist=0123456789'
            texto = pytesseract.image_to_string(image, config=custom_config).strip()
            # Limpiar cualquier cosa que no sea un dígito
            texto_limpio = ''.join(c for c in texto if c.isdigit())
            self.logger.info(f"Texto OCR del CAPTCHA (original): '{texto}'")
            self.logger.info(f"Texto OCR del CAPTCHA (limpio): '{texto_limpio}'")
            # Validar longitud si sabes que siempre debe ser X dígitos
            # Por ejemplo, si el CAPTCHA suele ser de 4 a 6 dígitos:
            if len(texto_limpio) == 6:  # Validar que sea exactamente 6 dígitos
                self.logger.info(f"Texto OCR del CAPTCHA (validado): '{texto_limpio}'")
                return texto_limpio
            else:
                self.logger.warning(f"Texto OCR del CAPTCHA tiene longitud inusual: '{texto_limpio}' (longitud: {len(texto_limpio)}). Se considera inválido.")
                return ""  # Devolver cadena vacía si la longitud no parece correcta

        except Exception as e:
            self.logger.error(f"Error al resolver CAPTCHA con OCR: {str(e)}")
            return ""

    def interactuar_con_formulario(self, identificador, ano_nacimiento, captcha_texto):
        try:
            # Esperar y localizar elementos del formulario
            # Esperar que el select esté presente y clickeable
            tipo_tramite_select_element = self.wait.until(
                EC.element_to_be_clickable((By.ID, "infServicio"))
            )

            # Esperar que la opción "VISADO" esté presente dentro del select
            # Usamos un XPath para esperar específicamente por la opción con value="VISADO"
            self.wait.until(
                EC.presence_of_element_located((By.XPATH, "//select[@id='infServicio']/option[@value='VISADO']"))
            )

            # Interactuar con el select usando el valor
            from selenium.webdriver.support.ui import Select
            select = Select(tipo_tramite_select_element)
            # Seleccionar por valor en lugar de texto visible
            select.select_by_value("VISADO")

            # Esperar que los demás inputs estén presentes y visibles
            identificador_input = self.wait.until(EC.presence_of_element_located((By.ID, "txIdentificador")))
            ano_nacimiento_input = self.wait.until(EC.presence_of_element_located((By.ID, "txtFechaNacimiento")))
            captcha_input = self.wait.until(EC.presence_of_element_located((By.ID, "imgcaptcha")))
            submit_button = self.wait.until(EC.element_to_be_clickable((By.ID, "imgVerSuTramite")))

            # Interactuar con los inputs
            identificador_input.clear()
            identificador_input.send_keys(identificador)
            ano_nacimiento_input.clear()
            ano_nacimiento_input.send_keys(ano_nacimiento)
            captcha_input.clear()
            captcha_input.send_keys(captcha_texto)

            # Hacer clic en el botón de submit
            submit_button.click()
            self.logger.info(f"Formulario enviado para {identificador}.")
            return True
        except (TimeoutException, NoSuchElementException) as e:
            self.logger.error(f"Error al interactuar con el formulario para {identificador}: {str(e)}")
            return False

    def extraer_estado(self):
        try:
            # Esperar a que el contenedor del estado esté presente
            self.wait.until(EC.presence_of_element_located((By.ID, "CajaGenerica")))

            # Esperar a que el título tenga algún texto (no vacío)
            self.wait.until(
                lambda driver: driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado").text.strip() != ""
            )
            # Esperar a que la descripción tenga algún texto
            self.wait.until(
                lambda driver: driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado").text.strip() != ""
            )

            # Ahora sí obtener los elementos y sus textos
            titulo_element = self.driver.find_element(By.ID, "ContentPlaceHolderConsulta_TituloEstado")
            desc_element = self.driver.find_element(By.ID, "ContentPlaceHolderConsulta_DescEstado")

            titulo_estado = titulo_element.text.strip().upper()
            desc_estado = desc_element.text.strip()
            estado_completo = f"{titulo_estado} - {desc_estado}"

            self.logger.info(f"Estado extraído: {estado_completo}")
            return estado_completo

        except (TimeoutException, NoSuchElementException) as e:
            self.logger.error(f"Error al extraer el estado: {str(e)}")
            # Intentar detectar error de CAPTCHA
            try:
                error_captcha_element = self.driver.find_element(By.ID, "CompararCaptcha")
                if error_captcha_element.is_displayed():
                    error_text = error_captcha_element.text
                    self.logger.warning(f"Mensaje de error del servidor (posiblemente CAPTCHA incorrecto): {error_text}")
                    return None
            except NoSuchElementException:
                self.logger.info("No se encontró mensaje de error de CAPTCHA específico.")
            return None

    # --- FUNCIONES PARA GESTIONAR ARCHIVOS POR CUENTA ---
    def _get_estado_file(self, identificador):
        return f"estado_tramite_{identificador}.json"

    def _get_historial_file(self, identificador):
        return f"historial_verificaciones_{identificador}.json"

    def guardar_estado(self, identificador, estado):
        estado_file = self._get_estado_file(identificador)
        with open(estado_file, 'w', encoding='utf-8') as f:
            json.dump({"ultimo_estado": estado, "timestamp": time.time()}, f, ensure_ascii=False, indent=4)
        self.logger.info(f"Estado guardado en {estado_file} para {identificador}: {estado}")

    def cargar_estado_anterior(self, identificador):
        estado_file = self._get_estado_file(identificador)
        estado_anterior = None
        if os.path.exists(estado_file):
            try:
                with open(estado_file, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    estado_anterior = data.get("ultimo_estado")
                    self.logger.info(f"Estado anterior cargado para {identificador}: {estado_anterior}")
            except (json.JSONDecodeError, KeyError) as e:
                self.logger.error(f"Error al cargar el estado anterior para {identificador}: {e}")
        else:
            self.logger.info(f"Archivo de estado anterior no encontrado para {identificador}.")
        return estado_anterior

    def cargar_historial(self, identificador):
        historial_file = self._get_historial_file(identificador)
        historial = []
        if os.path.exists(historial_file):
            try:
                with open(historial_file, 'r', encoding='utf-8') as f:
                    historial = json.load(f)
                    self.logger.info(f"Historial cargado para {identificador}. Total: {len(historial)} entradas.")
            except (json.JSONDecodeError, KeyError) as e:
                self.logger.error(f"Error al cargar historial para {identificador}: {e}")
        else:
            self.logger.info(f"Archivo de historial no encontrado para {identificador}.")
        return historial

    def guardar_historial(self, identificador, historial):
        historial_file = self._get_historial_file(identificador)
        with open(historial_file, 'w', encoding='utf-8') as f:
            json.dump(historial, f, ensure_ascii=False, indent=4)
        self.logger.info(f"Historial guardado en {historial_file} para {identificador}. Total entradas: {len(historial)}")

    def registrar_verificacion(self, identificador, estado, exitoso=True):
        historial = self.cargar_historial(identificador) # Cargar el historial existente para la cuenta
        entrada = {
            "fecha_hora": time.strftime('%Y-%m-%d %H:%M:%S'),
            "estado": estado,
            "exitoso": exitoso
        }
        historial.append(entrada)
        self.guardar_historial(identificador, historial) # Guardar el historial actualizado
    # --- FIN FUNCIONES POR CUENTA ---

    # --- FUNCIONES DE NOTIFICACION MODIFICADAS PARA USAR CORREO POR CUENTA ---
    def _get_email_destino(self, identificador):
        """Obtiene el correo de destino para una cuenta específica."""
        for cuenta in self.cuentas:
            if cuenta['identificador'] == identificador:
                return cuenta.get('email_notif', self.config['notificaciones'].get('email_destino')) # Busca el correo específico, sino usa el general
        return self.config['notificaciones'].get('email_destino') # Por si acaso no se encuentra la cuenta (aunque debería)

    def enviar_notificacion(self, asunto, cuerpo, identificador_destino, es_html=False):
        """Envía una notificación al correo asociado a un identificador específico."""
        email_destino = self._get_email_destino(identificador_destino)
        if not email_destino:
            self.logger.error(f"No se encontró un correo de destino para {identificador_destino}. No se envía notificación.")
            return

        try:
            msg = MIMEMultipart('alternative' if es_html else 'mixed')
            msg['From'] = self.config['notificaciones']['email_origen']
            msg['To'] = email_destino  # Usar el correo obtenido
            msg['Subject'] = asunto

            if es_html:
                # Si es HTML, usamos 'alternative' y adjuntamos el cuerpo HTML
                parte_html = MIMEText(cuerpo, 'html', 'utf-8')
                msg.attach(parte_html)
            else:
                # Si es texto plano, usamos 'mixed' y adjuntamos el cuerpo como texto
                parte_texto = MIMEText(cuerpo, 'plain', 'utf-8')
                msg.attach(parte_texto)

            server = smtplib.SMTP('smtp.gmail.com', 587)
            server.starttls()
            server.login(self.config['notificaciones']['email_origen'], self.config['notificaciones']['app_password'])
            text = msg.as_string()
            server.sendmail(self.config['notificaciones']['email_origen'], email_destino, text) # Usar el correo específico
            server.quit()
            self.logger.info(f"Notificación enviada a {email_destino} para {identificador_destino}: {asunto}")
        except Exception as e:
            self.logger.error(f"Error al enviar notificación a {email_destino} para {identificador_destino}: {e}")

    # No se modifica generar_resumen_html, pero se puede adaptar para resumir por cuenta si es necesario
    # Por ahora, se mantiene como estaba, generando un resumen general o por cuenta si se llama específicamente
    # --- FIN MODIFICACIONES NOTIFICACION ---

    def consultar_estado_para_cuenta(self, identificador, ano_nacimiento): # Nuevo nombre
        max_reintentos_captcha = 12
        intentos = 0
        while intentos < max_reintentos_captcha:
            self.logger.info(f"Consultando estado para {identificador} - Intento {intentos + 1} de {max_reintentos_captcha}")
            try:
                # Volver a la página principal antes de cada intento para una nueva consulta
                self.driver.get("https://sutramiteconsular.maec.es/  ")
                time.sleep(2 if intentos < 6 else 1)

                captcha_path = self.capturar_captcha()
                if not captcha_path:
                    intentos += 1
                    time.sleep(5)
                    continue

                processed_path = self.preprocesar_captcha(captcha_path)
                captcha_texto = self.resolver_captcha(processed_path)

                # Eliminar archivos temporales *después* de intentar resolver el CAPTCHA
                for path in [captcha_path, processed_path]:
                    if os.path.exists(path):
                        os.remove(path)

                if not captcha_texto:
                    intentos += 1
                    time.sleep(5)
                    continue

                if not self.interactuar_con_formulario(identificador, ano_nacimiento, captcha_texto):
                    intentos += 1
                    time.sleep(5)
                    continue

                estado = self.extraer_estado()

                if estado is not None:
                    self.registrar_verificacion(identificador, estado, exitoso=True)
                    return estado # Devuelve el estado si se obtiene
                else:
                    self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                    intentos += 1
                    time.sleep(5)
            except WebDriverException as e:
                self.logger.error(f"Error de WebDriver durante la consulta para {identificador}: {str(e)}")
                self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                intentos += 1
                time.sleep(5)
            except Exception as e:
                self.logger.error(f"Error inesperado durante la consulta para {identificador}: {str(e)}")
                self.registrar_verificacion(identificador, "ERROR", exitoso=False)
                intentos += 1
                time.sleep(5)
        self.logger.error(f"Consulta fallida para {identificador} tras todos los reintentos.")
        return None

    def ejecutar_monitoreo(self):
        self.logger.info("Iniciando ciclo de monitoreo para múltiples cuentas.")
        for cuenta in self.cuentas:
            identificador = cuenta['identificador']
            ano_nacimiento = cuenta['año_nacimiento']
            # email_notif = cuenta.get('email_notif') # No es necesario aquí si se obtiene en enviar_notificacion
            self.logger.info(f"Procesando cuenta: {identificador}")

            # Cargar estado y historial específicos de la cuenta
            estado_anterior = self.cargar_estado_anterior(identificador)

            estado_actual = self.consultar_estado_para_cuenta(identificador, ano_nacimiento)
            if estado_actual is not None:
                hay_cambio = estado_actual != estado_anterior
                es_primera_vez = estado_anterior is None

                if hay_cambio or es_primera_vez:
                    self.guardar_estado(identificador, estado_actual)
                    if es_primera_vez:
                        asunto = f"[BOT Visado] 🎉 Estado Inicial para {identificador}"
                        cuerpo = f"""
                        ¡Hola! Este es el estado inicial de tu trámite con identificador {identificador}.
                        Estado: {estado_actual}
                        Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}
                        Enlace: https://sutramiteconsular.maec.es/
                        El bot seguirá monitoreando.
                        """
                    else:
                        asunto = f"[BOT Visado] 🚨 Cambio de Estado para {identificador}: {estado_actual}"
                        cuerpo = f"""
                        El estado de tu trámite con identificador {identificador} ha cambiado.
                        Nuevo Estado: {estado_actual}
                        Fecha: {time.strftime('%Y-%m-%d %H:%M:%S')}
                        Enlace: https://sutramiteconsular.maec.es/
                        """
                    # Enviar notificación usando el correo específico de la cuenta
                    self.enviar_notificacion(asunto, cuerpo, identificador)
                else:
                    self.logger.info(f"Sin cambios para {identificador}: {estado_actual}")
            else:
                self.logger.warning(f"No se obtuvo estado válido para {identificador}.")

        self.logger.info("Ciclo de monitoreo para todas las cuentas completado.")

    def iniciar(self):
        intervalo = self.config.get('intervalo_horas', 0.5) * 3600
        schedule.every(intervalo).seconds.do(self.ejecutar_monitoreo)
        # Opcional: enviar un resumen general cada 12 horas
        # schedule.every(12).hours.do(self.enviar_resumen_diario) # Puedes adaptar esta función también
        self.logger.info(f"Monitoreo para {len(self.cuentas)} cuentas cada {intervalo/60:.1f} minutos.")
        self.ejecutar_monitoreo()  # Primera ejecución inmediata
        while True:
            schedule.run_pending()
            time.sleep(60)

    def cerrar(self):
        if self.driver:
            self.driver.quit()
            self.logger.info("Navegador cerrado.")

if __name__ == "__main__":
    bot = BotVisado()
    try:
        bot.inicializar_selenium()
        bot.iniciar()
    except KeyboardInterrupt:
        print("\nInterrupción del usuario.")
        bot.logger.info("Cerrando bot...")
    except Exception as e:
        bot.logger.error(f"Error fatal: {e}")
    finally:
        bot.cerrar()

# config.yaml
intervalo_horas: 0.5

notificaciones:
  email_origen: "conabinotificaciones@gmail.com"
  app_password: "jzctkhlbxtmchlig"
  email_destinos:
    - "eduardodanielperezruiz@gmail.com"
    - "edua56621636@gmail.com"

cuentas:
  - nombre: "Eduardo Pérez"
    identificador: "ESP326CU6B42408511646445"
    año_nacimiento: "2005"
    email_notif: "eduardodanielperezruiz@gmail.com"

  - nombre: "Lucía Soler"
    identificador: "ESP326CUDB42108243467929"
    año_nacimiento: "2006"
    email_notif: "edua56621636@gmail.com"



# Ejecutar el bot
CMD ["python", "bot_visado.py"]
