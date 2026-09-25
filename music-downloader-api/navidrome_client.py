import hashlib
import logging
import secrets
import time
from typing import Dict, Any, Optional, List
import requests
from config import settings

logger = logging.getLogger("music-downloader.navidrome")


class NavidromeClient:
    def __init__(self):
        self.base_url = settings.NAVIDROME_URL.rstrip("/")
        self.username = settings.NAVIDROME_ADMIN_USER
        self.password = settings.NAVIDROME_ADMIN_PASSWORD
        self._jwt_token: Optional[str] = None
        self._token_time: float = 0.0

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

    # =========================================================================
    # RESCAN & SCAN STATUS (SUBSONIC API)
    # =========================================================================

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

    # =========================================================================
    # NATIVE API AUTH (JWT)
    # =========================================================================

    def _get_jwt_token(self) -> Optional[str]:
        if not self.password:
            return None
        now = time.time()
        if self._jwt_token and (now - self._token_time < 3600):
            return self._jwt_token

        try:
            url = f"{self.base_url}/auth/login"
            payload = {"username": self.username, "password": self.password}
            resp = requests.post(url, json=payload, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                token = data.get("token")
                if token:
                    self._jwt_token = token
                    self._token_time = now
                    return token
            logger.warning(f"Logowanie do Navidrome Native API ({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.warning(f"Błąd połączenia z Navidrome Native Auth: {e}")
        return None

    def _native_headers(self) -> Dict[str, str]:
        token = self._get_jwt_token()
        headers = {"Content-Type": "application/json"}
        if token:
            headers["X-ND-Authorization"] = f"Bearer {token}"
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # =========================================================================
    # USER MANAGEMENT (NAVIDROME NATIVE API)
    # =========================================================================

    def get_users(self) -> List[Dict[str, Any]]:
        """Zwraca listę wszystkich użytkowników z Navidrome."""
        if not self.password:
            return []
        token = self._get_jwt_token()
        if not token:
            return []

        try:
            url = f"{self.base_url}/api/user"
            resp = requests.get(url, headers=self._native_headers(), timeout=10)
            if resp.status_code == 200:
                return resp.json() or []
            logger.warning(f"Błąd get_users ({resp.status_code}): {resp.text}")
        except Exception as e:
            logger.error(f"Błąd pobierania użytkowników: {e}")
        return []

    def create_user(
        self,
        username: str,
        name: str,
        password: str,
        email: str = "",
        is_admin: bool = False
    ) -> Dict[str, Any]:
        """Tworzy nowego użytkownika w Navidrome."""
        if not self.password:
            raise RuntimeError("Brak hasła administratora w konfiguracji NAVIDROME_ADMIN_PASSWORD.")

        token = self._get_jwt_token()
        if not token:
            raise RuntimeError("Nie udało się zalogować do Navidrome jako administrator.")

        url = f"{self.base_url}/api/user"
        payload = {
            "userName": username.strip(),
            "name": name.strip() or username.strip(),
            "password": password,
            "email": email.strip(),
            "isAdmin": bool(is_admin)
        }
        resp = requests.post(url, json=payload, headers=self._native_headers(), timeout=10)
        if resp.status_code in [200, 201]:
            return resp.json()
        raise RuntimeError(f"Błąd tworzenia użytkownika ({resp.status_code}): {resp.text}")

    def update_user(
        self,
        user_id: str,
        username: Optional[str] = None,
        name: Optional[str] = None,
        password: Optional[str] = None,
        email: Optional[str] = None,
        is_admin: Optional[bool] = None
    ) -> Dict[str, Any]:
        """Aktualizuje dane użytkownika (np. hasło, uprawnienia administratora)."""
        if not self.password:
            raise RuntimeError("Brak hasła administratora w konfiguracji NAVIDROME_ADMIN_PASSWORD.")

        token = self._get_jwt_token()
        if not token:
            raise RuntimeError("Nie udało się zalogować do Navidrome jako administrator.")

        # Pobierz aktualne dane użytkownika, aby zachować niemodyfikowane pola
        current_user = None
        users = self.get_users()
        for u in users:
            if u.get("id") == user_id or u.get("userName") == username:
                current_user = u
                user_id = u.get("id", user_id)
                break

        payload: Dict[str, Any] = {"id": user_id}
        if current_user:
            payload["userName"] = current_user.get("userName")
            payload["name"] = current_user.get("name")
            payload["email"] = current_user.get("email", "")
            payload["isAdmin"] = current_user.get("isAdmin", False)

        if username is not None:
            payload["userName"] = username.strip()
        if name is not None:
            payload["name"] = name.strip()
        if email is not None:
            payload["email"] = email.strip()
        if is_admin is not None:
            payload["isAdmin"] = bool(is_admin)
        if password:
            payload["password"] = password

        url = f"{self.base_url}/api/user/{user_id}"
        resp = requests.put(url, json=payload, headers=self._native_headers(), timeout=10)
        if resp.status_code in [200, 204]:
            return resp.json() if resp.text else payload
        raise RuntimeError(f"Błąd aktualizacji użytkownika ({resp.status_code}): {resp.text}")

    def delete_user(self, user_id: str) -> bool:
        """Usuwa użytkownika z Navidrome."""
        if not self.password:
            raise RuntimeError("Brak hasła administratora w konfiguracji NAVIDROME_ADMIN_PASSWORD.")

        token = self._get_jwt_token()
        if not token:
            raise RuntimeError("Nie udało się zalogować do Navidrome jako administrator.")

        url = f"{self.base_url}/api/user/{user_id}"
        resp = requests.delete(url, headers=self._native_headers(), timeout=10)
        if resp.status_code in [200, 204]:
            return True
        raise RuntimeError(f"Błąd usuwania użytkownika ({resp.status_code}): {resp.text}")

    # =========================================================================
    # PLAYLIST MANAGEMENT (SUBSONIC API)
    # =========================================================================

    def get_playlists(self) -> List[Dict[str, Any]]:
        """Pobiera listę wszystkich playlist."""
        if not self.password:
            return []

        url = f"{self.base_url}/rest/getPlaylists.view"
        params = self._get_auth_params()

        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("subsonic-response", {})
                pls = data.get("playlists", {}).get("playlist", [])
                if isinstance(pls, dict):
                    pls = [pls]
                return pls
        except Exception as e:
            logger.error(f"Błąd pobierania playlist: {e}")
        return []

    def get_playlist(self, playlist_id: str) -> Dict[str, Any]:
        """Pobiera szczegóły playlisty wraz z listą utworów."""
        if not self.password:
            return {}

        url = f"{self.base_url}/rest/getPlaylist.view"
        params = self._get_auth_params()
        params["id"] = playlist_id

        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("subsonic-response", {})
                pl = data.get("playlist", {})
                entries = pl.get("entry", [])
                if isinstance(entries, dict):
                    entries = [entries]
                pl["tracks"] = entries
                return pl
        except Exception as e:
            logger.error(f"Błąd pobierania playlisty {playlist_id}: {e}")
        return {}

    def create_playlist(self, name: str, song_ids: Optional[List[str]] = None) -> Dict[str, Any]:
        """Tworzy nową playlistę w Navidrome."""
        if not self.password:
            raise RuntimeError("Brak hasła administratora w NAVIDROME_ADMIN_PASSWORD.")

        url = f"{self.base_url}/rest/createPlaylist.view"
        params: List[tuple] = list(self._get_auth_params().items())
        params.append(("name", name.strip()))
        if song_ids:
            for s_id in song_ids:
                params.append(("songId", s_id))

        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("subsonic-response", {})
            if data.get("status") == "ok":
                return data.get("playlist", {})
            err = data.get("error", {}).get("message", "Nieznany błąd")
            raise RuntimeError(err)
        raise RuntimeError(f"Błąd HTTP {resp.status_code}: {resp.text}")

    def add_tracks_to_playlist(self, playlist_id: str, song_ids: List[str]) -> Dict[str, Any]:
        """Dodaje utwory do istniejącej playlisty."""
        if not self.password or not song_ids:
            return {}

        url = f"{self.base_url}/rest/updatePlaylist.view"
        params: List[tuple] = list(self._get_auth_params().items())
        params.append(("playlistId", playlist_id))
        for s_id in song_ids:
            params.append(("songIdToAdd", s_id))

        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("subsonic-response", {})
            if data.get("status") == "ok":
                return {"status": "success"}
            err = data.get("error", {}).get("message", "Nieznany błąd")
            raise RuntimeError(err)
        raise RuntimeError(f"Błąd HTTP {resp.status_code}: {resp.text}")

    def remove_track_from_playlist(self, playlist_id: str, song_index: int) -> Dict[str, Any]:
        """Usuwa utwór o danym indeksie z playlisty."""
        if not self.password:
            return {}

        url = f"{self.base_url}/rest/updatePlaylist.view"
        params = self._get_auth_params()
        params["playlistId"] = playlist_id
        params["songIndexToRemove"] = str(song_index)

        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("subsonic-response", {})
            if data.get("status") == "ok":
                return {"status": "success"}
            err = data.get("error", {}).get("message", "Nieznany błąd")
            raise RuntimeError(err)
        raise RuntimeError(f"Błąd HTTP {resp.status_code}: {resp.text}")

    def delete_playlist(self, playlist_id: str) -> bool:
        """Usuwa playlistę."""
        if not self.password:
            return False

        url = f"{self.base_url}/rest/deletePlaylist.view"
        params = self._get_auth_params()
        params["id"] = playlist_id

        resp = requests.get(url, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json().get("subsonic-response", {})
            return data.get("status") == "ok"
        return False

    def search_songs(self, query: str, count: int = 30) -> List[Dict[str, Any]]:
        """Wyszukuje utwory w bazie Navidrome (Subsonic search3)."""
        if not self.password or not query.strip():
            return []

        url = f"{self.base_url}/rest/search3.view"
        params = self._get_auth_params()
        params["query"] = query.strip()
        params["songCount"] = str(count)

        try:
            resp = requests.get(url, params=params, timeout=10)
            if resp.status_code == 200:
                data = resp.json().get("subsonic-response", {})
                songs = data.get("searchResult3", {}).get("song", [])
                if isinstance(songs, dict):
                    songs = [songs]
                return songs
        except Exception as e:
            logger.error(f"Błąd wyszukiwania utworów w Navidrome: {e}")
        return []


navidrome_client = NavidromeClient()
