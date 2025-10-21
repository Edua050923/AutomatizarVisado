# Dockerfile
FROM python:3.9-slim

WORKDIR /app

# Instalar dependencias del sistema (MÉTODO CORREGIDO)
RUN apt-get update && apt-get install -y \
    wget \
    gnupg \
    tesseract-ocr \
    tesseract-ocr-spa \
    ca-certificates \
    # Crear el directorio para las claves de apt
    && install -m 0755 -d /etc/apt/keyrings \
    # Descargar la clave de Google, convertirla (dearmor) y guardarla en el keyring
    && wget -q -O - https://dl-ssl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /etc/apt/keyrings/google-chrome.gpg \
    # Asegurar que la clave sea legible por apt
    && chmod a+r /etc/apt/keyrings/google-chrome.gpg \
    # Añadir el repositorio de Chrome, indicando dónde está la clave
    && echo "deb [arch=amd64 signed-by=/etc/apt/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google.list \
    # Actualizar apt de nuevo y ahora sí instalar Chrome
    && apt-get update \
    && apt-get install -y google-chrome-stable \
    # Limpiar
    && rm -rf /var/lib/apt/lists/*

# Copiar requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar código (SOLO esta línea)
COPY . .

# Ejecutar el bot
CMD ["python", "bot_visado.py"]
