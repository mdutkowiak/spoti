import logging
import requests
from config import settings

logger = logging.getLogger("music-downloader.telegram")


class TelegramNotifier:
    @staticmethod
    def send_message(text: str):
        if not settings.TELEGRAM_BOT_TOKEN or not settings.TELEGRAM_CHAT_ID:
            return

        url = f"https://api.telegram.org/bot{settings.TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": settings.TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "Markdown"
        }

        try:
            resp = requests.post(url, json=payload, timeout=8)
            if resp.status_code != 200:
                logger.warning(f"Błąd wysyłania powiadomienia Telegram: {resp.text}")
        except Exception as e:
            logger.warning(f"Wyjątek podczas wysyłania do Telegram: {e}")


telegram_notifier = TelegramNotifier()
