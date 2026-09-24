#!/usr/bin/env python3
"""
Przykładowy, lekki bot Telegram do zdalnego zamawiania muzyki do biblioteki Navidrome.
Uruchamianie:
    python telegram_bot.py
lub jako serwis systemd / kontener.

Gdy wyślesz botowi link ze Spotify (np. utwór lub album), bot:
1. Przekaże zadanie do Music Downloader API.
2. Zwróci informację o rozpoczęciu pobierania.
3. Po zakończeniu wyśle powiadomienie z potwierdzeniem.
"""

import os
import sys
import time
import requests

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
API_URL = os.getenv("API_URL", "http://localhost:8000")
POLL_INTERVAL = 2

if not TELEGRAM_TOKEN:
    print("BŁĄD: Ustaw zmienną środowiskową TELEGRAM_BOT_TOKEN!")
    sys.exit(1)

BASE_TG_URL = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"


def send_message(chat_id: int, text: str):
    requests.post(
        f"{BASE_TG_URL}/sendMessage",
        json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"},
        timeout=10
    )


def handle_message(chat_id: int, text: str):
    text = text.strip()
    if text.startswith("/start") or text.startswith("/help"):
        send_message(
            chat_id,
            "👋 *Witaj w Spoti-Downloader Bot!*\n\n"
            "Wyślij mi dowolny link ze Spotify (utwór, album, playlista) lub wpisz nazwę utworu:\n"
            "`Artysta - Tytuł`\n\n"
            "Pobiorę go natychmiast w zoptymalizowanym formacie Opus (~160 kbps), "
            "otaguję, osadzę okładkę i odświeżę Twojego Navidrome!"
        )
        return

    send_message(chat_id, f"⏳ *Przyjęto zlecenie:* `{text}`\nRozpoczynam pobieranie...")

    try:
        # Zlecenie do API
        res = requests.post(
            f"{API_URL}/download",
            json={"query_or_url": text, "format": "opus", "force": False},
            timeout=15
        )
        if res.status_code != 202:
            send_message(chat_id, f"❌ Błąd API: {res.text}")
            return

        task_id = res.json().get("task_id")

        # Śledzenie zadania
        while True:
            t_res = requests.get(f"{API_URL}/tasks/{task_id}", timeout=10)
            task_data = t_res.json()
            status = task_data.get("status")

            if status == "success":
                total = task_data.get("completed_tracks", 1)
                send_message(
                    chat_id,
                    f"✅ *Sukces!* Pobrano {total} utworów.\n"
                    f"Piosenki są już dostępne w Navidrome i Symfonium na Twoim telefonie/CarPlay!"
                )
                break
            elif status == "failed":
                err = task_data.get("error_message", "Nieznany błąd")
                send_message(chat_id, f"❌ *Błąd pobierania:* {err}")
                break

            time.sleep(POLL_INTERVAL)

    except Exception as e:
        send_message(chat_id, f"❌ Błąd krytyczny: {str(e)}")


def main():
    print("Bot Telegram uruchomiony. Oczekiwanie na wiadomości...")
    offset = 0
    while True:
        try:
            resp = requests.get(f"{BASE_TG_URL}/getUpdates", params={"offset": offset, "timeout": 30}, timeout=40)
            data = resp.json()
            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message", {})
                chat_id = msg.get("chat", {}).get("id")
                text = msg.get("text")
                if chat_id and text:
                    handle_message(chat_id, text)
        except Exception as e:
            time.sleep(5)


if __name__ == "__main__":
    main()
