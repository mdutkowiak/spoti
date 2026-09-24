import hashlib
import logging
import secrets
from typing import Dict, Any, Optional
import requests
from config import settings

logger = logging.getLogger("music-downloader.navidrome")


class NavidromeClient:
    def __init__(self):
        self.base_url = settings.NAVIDROME_URL.rstrip("/")
        self.username = settings.NAVIDROME_ADMIN_USER
        self.password = settings.NAVIDROME_ADMIN_PASSWORD

    def _get_auth_params(self) -> Dict[str, str]:
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode("utf-8")).hexdigest()
        return {
            "u": self.username,
            "t": token,
            "s": salt,
            "v": "1.16.1",
            "c": "music-downloader-api",
            "f": "json"
        }

    def trigger_scan(self, full_scan: bool = False) -> Dict[str, Any]:
        """
        Wywołuje Subsonic API startScan.view, aby Navidrome natychmiast
        zarejestrował nowo pobrane utwory w bazie danych.
        """
        if not self.password:
            logger.warning("Brak skonfigurowanego hasła NAVIDROME_ADMIN_PASSWORD. Automatyczny reskan pominięty.")
            return {"status": "skipped", "message": "Admin password not configured"}

        url = f"{self.base_url}/rest/startScan.view"
        params = self._get_auth_params()
        if full_scan:
            params["fullScan"] = "true"

        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                subsonic_res = data.get("subsonic-response", {})
                if subsonic_res.get("status") == "ok":
                    logger.info("Pomyślnie wywołano natychmiastowy reskan Navidrome.")
                    return {"status": "success", "data": subsonic_res.get("scanStatus", {})}
                else:
                    error_info = subsonic_res.get("error", {})
                    logger.error(f"Błąd odpowiedzi Subsonic API: {error_info}")
                    return {"status": "error", "error": error_info}
            else:
                logger.error(f"Błąd HTTP {resp.status_code} podczas wywołania startScan: {resp.text}")
                return {"status": "error", "http_status": resp.status_code}
        except Exception as e:
            logger.error(f"Błąd połączenia z Navidrome ({url}): {e}")
            return {"status": "error", "exception": str(e)}

    def get_scan_status(self) -> Dict[str, Any]:
        if not self.password:
            return {"status": "unconfigured"}

        url = f"{self.base_url}/rest/getScanStatus.view"
        params = self._get_auth_params()

        try:
            resp = requests.get(url, params=params, timeout=5)
            if resp.status_code == 200:
                return resp.json().get("subsonic-response", {}).get("scanStatus", {})
        except Exception as e:
            logger.error(f"Błąd pobierania statusu skanu: {e}")
        return {}


navidrome_client = NavidromeClient()
