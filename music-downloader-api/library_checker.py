import os
import re
import time
import logging
from typing import Set, Tuple
from config import settings

logger = logging.getLogger("music-downloader.library")


class LibraryChecker:
    def __init__(self, music_dir: str):
        self.music_dir = music_dir
        self._last_scan = 0.0
        self._cache: Set[Tuple[str, str]] = set()
        self._title_cache: Set[str] = set()

    def invalidate(self):
        """Wymusza odswiezenie indeksu przy nastepnym sprawdzeniu."""
        self._last_scan = 0.0
        self._cache.clear()
        self._title_cache.clear()

    @staticmethod
    def _normalize(text: str) -> str:
        if not text:
            return ""
        cleaned = re.sub(r"[\(\[].*?[\)\]]", "", text)
        cleaned = re.sub(r"[^\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", "", cleaned.lower())
        return cleaned

    def refresh_if_needed(self, max_age_seconds: int = 5):
        now = time.time()
        if now - self._last_scan < max_age_seconds and (self._cache or self._title_cache):
            return

        if not os.path.exists(self.music_dir):
            self._cache.clear()
            self._title_cache.clear()
            self._last_scan = now
            return

        new_cache: Set[Tuple[str, str]] = set()
        new_title_cache: Set[str] = set()

        audio_exts = {".opus", ".mp3", ".flac", ".m4a", ".ogg", ".wav"}
        try:
            for root, _, files in os.walk(self.music_dir):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in audio_exts:
                        name_no_ext = os.path.splitext(f)[0]
                        clean_filename = re.sub(r"^\d+\s*[-_.]\s*", "", name_no_ext)

                        rel = os.path.relpath(root, self.music_dir)
                        parts = rel.split(os.sep)
                        artist = parts[0] if len(parts) >= 1 and parts[0] not in [".", "Playlists"] else ""

                        norm_t = self._normalize(clean_filename)
                        norm_a = self._normalize(artist)

                        if norm_t:
                            new_title_cache.add(norm_t)
                            if norm_a:
                                new_cache.add((norm_a, norm_t))

                        if " - " in clean_filename:
                            parts_file = clean_filename.split(" - ", 1)
                            norm_fa = self._normalize(parts_file[0])
                            norm_ft = self._normalize(parts_file[1])
                            if norm_ft:
                                new_title_cache.add(norm_ft)
                                if norm_fa:
                                    new_cache.add((norm_fa, norm_ft))
        except Exception as e:
            logger.warning(f"Blad skanowania katalogu muzycznego ({self.music_dir}): {e}")

        self._cache = new_cache
        self._title_cache = new_title_cache
        self._last_scan = now

    def contains(self, artist: str, title: str) -> bool:
        if not title:
            return False

        self.refresh_if_needed()
        norm_t = self._normalize(title)
        norm_a = self._normalize(artist)

        if not norm_t:
            return False

        # 1. Dokladna para (artysta, tytul)
        if (norm_a, norm_t) in self._cache:
            return True

        # 2. Jesli tytul jest w bibliotece, a wykonawca czesciowo pasuje
        if norm_t in self._title_cache:
            if not norm_a:
                return True
            for c_art, c_tit in self._cache:
                if c_tit == norm_t:
                    if norm_a in c_art or c_art in norm_a:
                        return True
                    if len(norm_a) >= 3 and norm_a[:4] in c_art:
                        return True

        return False


library_checker = LibraryChecker(settings.MUSIC_DIR)
