FROM python:3.11-slim

# Ustawienia zmiennych środowiskowych Pythona
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DEBIAN_FRONTEND=noninteractive

# Instalacja ffmpeg, curl i narzędzi systemowych
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Utworzenie katalogu aplikacji
WORKDIR /app

# Kopiowanie i instalacja zależności
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Przygotowanie katalogów roboczych z właściwymi uprawnieniami
RUN mkdir -p /music /data/downloader && chmod -R 777 /music /data

# Kopiowanie kodu aplikacji
COPY . .

# Port wystawiany przez FastAPI
EXPOSE 8000

# Start serwera uvicorn
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
