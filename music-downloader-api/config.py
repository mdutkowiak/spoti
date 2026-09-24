import os
from pydantic_settings import BaseSettings
from pydantic import Field


class Settings(BaseSettings):
    # Ścieżki dyskowe
    MUSIC_DIR: str = Field(default="/music", description="Katalog biblioteki muzycznej zamontowany w kontenerze")
    DATA_DIR: str = Field(default="/data/downloader", description="Katalog na dane tymczasowe i bazę zadań")
    
    # Bezpieczeństwo API
    API_SECRET_KEY: str = Field(default="", description="Opcjonalny klucz API (jeśli podany, wymagany w nagłówku X-API-Key)")
    
    # Spotify Web API Credentials
    SPOTIFY_CLIENT_ID: str = Field(default="", description="Spotify Client ID z developer.spotify.com")
    SPOTIFY_CLIENT_SECRET: str = Field(default="", description="Spotify Client Secret")
    SPOTIFY_MARKET: str = Field(default="PL", description="Rynek Spotify dla dostępności utworów (np. PL, US)")
    
    # Preferencje audio
    DEFAULT_AUDIO_FORMAT: str = Field(default="opus", description="Domyślny format: opus, mp3, flac")
    AUDIO_BITRATE: str = Field(default="160k", description="Bitrate dla kodera ffmpeg (np. 160k dla opus, 320k dla mp3)")
    
    # Integracja z Navidrome
    NAVIDROME_URL: str = Field(default="http://navidrome:4533", description="Adres wewnętrzny serwera Navidrome")
    NAVIDROME_ADMIN_USER: str = Field(default="admin", description="Użytkownik administracyjny Navidrome")
    NAVIDROME_ADMIN_PASSWORD: str = Field(default="", description="Hasło administracyjne Navidrome")
    
    # Integracja z SLSKD (Soulseek FLAC)
    ENABLE_SLSKD: bool = Field(default=False, description="Czy włączyć integrację ze slskd pod kątem FLAC")
    SLSKD_URL: str = Field(default="http://slskd:5030", description="Adres API SLSKD")
    SLSKD_API_KEY: str = Field(default="", description="Klucz API SLSKD")
    
    # Powiadomienia Telegram
    TELEGRAM_BOT_TOKEN: str = Field(default="", description="Token bota Telegram do powiadomień")
    TELEGRAM_CHAT_ID: str = Field(default="", description="ID czatu do powiadomień")

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()

# Upewnienie się, że katalogi istnieją
for p in [settings.MUSIC_DIR, settings.DATA_DIR, os.path.join(settings.DATA_DIR, "temp")]:
    try:
        os.makedirs(p, exist_ok=True)
    except Exception:
        pass
