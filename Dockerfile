# SARA — Story And Reel Automator
# Python 3.11 + FFmpeg auf Debian Slim

FROM python:3.11-slim

# System-Pakete: ffmpeg, Schriften für drawtext-Filter
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        fonts-dejavu-core \
        fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

# Arbeitsverzeichnis
WORKDIR /app

# Abhängigkeiten zuerst (Layer-Caching)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Quellcode kopieren
COPY main.py .
COPY templates/ templates/

# Datenverzeichnisse anlegen (werden als Volume gemountet)
RUN mkdir -p data/backgrounds data/outputs data/tts

# Anwendungsport
EXPOSE 7842

# Starten via Python direkt (Gunicorn-Alternative für Entwicklung)
CMD ["python", "main.py"]
