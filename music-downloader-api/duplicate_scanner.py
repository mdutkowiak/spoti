import os
import re
import time
import logging
from typing import List, Dict, Any
from config import settings

logger = logging.getLogger("music-downloader.scanner")


class DuplicateScanner:
    def __init__(self, music_dir: str):
        self.music_dir = music_dir

    @staticmethod
    def _normalize_title(text: str) -> str:
        if not text:
            return ""
        # Usuń dopiski w nawiasach: (Official Video), [Official Audio], (feat. ...), [Remastered] itp.
        cleaned = re.sub(r"[\(\[].*?[\)\]]", "", text)
        # Usuń znaki specjalne i spacje, zamień na małe litery
        cleaned = re.sub(r"[^\w\u3040-\u309F\u30A0-\u30FF\u4E00-\u9FFF]", "", cleaned.lower())
        return cleaned.strip()

    @staticmethod
    def _format_bytes(num_bytes: int) -> str:
        for unit in ["B", "KB", "MB", "GB"]:
            if abs(num_bytes) < 1024.0:
                return f"{num_bytes:3.1f} {unit}"
            num_bytes /= 1024.0
        return f"{num_bytes:.1f} TB"

    @staticmethod
    def _format_seconds(seconds: float) -> str:
        if not seconds or seconds <= 0:
            return ""
        total = int(round(seconds))
        m = total // 60
        s = total % 60
        return f"{m}:{s:02d}"

    def _read_file_metadata(self, full_path: str, rel_path: str) -> Dict[str, Any]:
        ext = os.path.splitext(full_path)[1].lower()
        stat = os.stat(full_path)
        size = stat.st_size
        mtime = stat.st_mtime
        mtime_str = time.strftime("%Y-%m-%d %H:%M", time.localtime(mtime))

        title = ""
        artist = ""
        album = ""
        duration_sec = 0.0
        bitrate_kbps = 0

        # Próba odczytu tagów z pliku za pomocą mutagen
        try:
            import mutagen
            audio = mutagen.File(full_path)
            if audio is not None:
                if hasattr(audio, "info") and audio.info:
                    duration_sec = getattr(audio.info, "length", 0.0)
                    bitrate = getattr(audio.info, "bitrate", 0)
                    if bitrate:
                        bitrate_kbps = int(bitrate // 1000)

                # Wyciąganie tagów w zależności od formatu
                tags = getattr(audio, "tags", {}) or {}
                # Tytuł
                for k in ["title", "TIT2"]:
                    if k in tags:
                        val = tags[k]
                        title = str(val[0]) if isinstance(val, (list, tuple)) else str(val)
                        break

                # Wykonawca
                for k in ["artist", "TPE1", "albumartist", "TPE2"]:
                    if k in tags:
                        val = tags[k]
                        artist = str(val[0]) if isinstance(val, (list, tuple)) else str(val)
                        break

                # Album
                for k in ["album", "TALB"]:
                    if k in tags:
                        val = tags[k]
                        album = str(val[0]) if isinstance(val, (list, tuple)) else str(val)
                        break
        except Exception as e:
            logger.debug(f"Błąd czytania tagów mutagen z {rel_path}: {e}")

        # Fallback ze ścieżki i nazwy pliku jeśli tagi były puste
        filename_no_ext = os.path.splitext(os.path.basename(full_path))[0]
        clean_filename = re.sub(r"^\d+\s*[-_.]\s*", "", filename_no_ext)

        parts = rel_path.split(os.sep)
        if not artist and len(parts) >= 2:
            artist = parts[0]
        if not album and len(parts) >= 3:
            album = parts[1]

        if not title:
            if " - " in clean_filename:
                f_parts = clean_filename.split(" - ", 1)
                if not artist:
                    artist = f_parts[0].strip()
                title = f_parts[1].strip()
            else:
                title = clean_filename

        return {
            "rel_path": rel_path.replace("\\", "/"),
            "full_path": full_path,
            "filename": os.path.basename(full_path),
            "title": title or clean_filename,
            "artist": artist or "Nieznany wykonawca",
            "album": album or "Brak albumu",
            "format": ext.lstrip(".").upper(),
            "size_bytes": size,
            "size_str": self._format_bytes(size),
            "duration_str": self._format_seconds(duration_sec),
            "bitrate_kbps": bitrate_kbps,
            "mtime": mtime,
            "mtime_str": mtime_str
        }

    def find_duplicates(self) -> Dict[str, Any]:
        """
        Przeszukuje bibliotekę i grupuje utwory posiadające ten sam znormalizowany tytuł.
        Zwraca statystyki oraz listę grup duplikatów.
        """
        if not os.path.exists(self.music_dir):
            return {
                "total_duplicate_groups": 0,
                "total_duplicate_files": 0,
                "potential_wasted_bytes": 0,
                "potential_wasted_str": "0 B",
                "groups": []
            }

        audio_exts = {".opus", ".mp3", ".flac", ".m4a", ".ogg", ".wav"}
        title_groups: Dict[str, List[Dict[str, Any]]] = {}

        try:
            for root, _, files in os.walk(self.music_dir):
                for f in files:
                    ext = os.path.splitext(f)[1].lower()
                    if ext in audio_exts:
                        full_path = os.path.join(root, f)
                        rel_path = os.path.relpath(full_path, self.music_dir)
                        meta = self._read_file_metadata(full_path, rel_path)

                        norm_t = self._normalize_title(meta["title"])
                        if not norm_t:
                            norm_t = self._normalize_title(meta["filename"])

                        if norm_t:
                            if norm_t not in title_groups:
                                title_groups[norm_t] = []
                            title_groups[norm_t].append(meta)
        except Exception as e:
            logger.error(f"Błąd skanowania duplikatów: {e}")

        # Filtrujemy tylko grupy z >= 2 plikami
        duplicate_groups = []
        total_dup_files = 0
        potential_wasted_bytes = 0

        for norm_t, files in title_groups.items():
            if len(files) >= 2:
                # Sortuj pliki w grupie: najpierw format bezstratny/największy bitrate/rozmiar
                files.sort(key=lambda x: (x["size_bytes"]), reverse=True)

                display_title = files[0]["title"]
                total_dup_files += len(files)

                # Załóżmy, że użytkownik zostawia 1 plik z grupy (np. największy), a resztę usuwa
                group_bytes = [f["size_bytes"] for f in files]
                wasted = sum(group_bytes[1:])
                potential_wasted_bytes += wasted

                duplicate_groups.append({
                    "normalized_title": norm_t,
                    "display_title": display_title,
                    "count": len(files),
                    "files": files
                })

        # Sortuj grupy alfabetycznie po tytule
        duplicate_groups.sort(key=lambda g: g["display_title"].lower())

        return {
            "total_duplicate_groups": len(duplicate_groups),
            "total_duplicate_files": total_dup_files,
            "potential_wasted_bytes": potential_wasted_bytes,
            "potential_wasted_str": self._format_bytes(potential_wasted_bytes),
            "groups": duplicate_groups
        }

    def delete_files(self, rel_paths: List[str]) -> Dict[str, Any]:
        """
        Bezpiecznie usuwa wybrane pliki z biblioteki.
        Weryfikuje, czy pliki znajdują się wewnątrz MUSIC_DIR, zapobiegając atakom directory traversal.
        """
        deleted_count = 0
        freed_bytes = 0
        errors = []

        music_dir_abs = os.path.abspath(self.music_dir)

        for rel_p in rel_paths:
            if not rel_p or not isinstance(rel_p, str):
                continue

            # Bezpieczeństwo: upewnij się, że ścieżka nie ucieka z katalogu /music
            target_path = os.path.abspath(os.path.join(music_dir_abs, rel_p.strip().lstrip("/\\")))
            if not target_path.startswith(music_dir_abs):
                errors.append(f"Odrzucono niebezpieczną ścieżkę: {rel_p}")
                continue

            if os.path.isfile(target_path):
                try:
                    fsize = os.path.getsize(target_path)
                    os.remove(target_path)
                    deleted_count += 1
                    freed_bytes += fsize
                    logger.info(f"Usunięto duplikat: {target_path} ({fsize} B)")

                    # Opcjonalnie posprzątaj puste katalogi nadrzędne
                    parent_dir = os.path.dirname(target_path)
                    while parent_dir != music_dir_abs and parent_dir.startswith(music_dir_abs):
                        try:
                            if not os.listdir(parent_dir):
                                os.rmdir(parent_dir)
                                logger.info(f"Usunięto pusty katalog: {parent_dir}")
                                parent_dir = os.path.dirname(parent_dir)
                            else:
                                break
                        except Exception:
                            break

                except Exception as e:
                    errors.append(f"Nie udało się usunąć {rel_p}: {e}")
            else:
                logger.warning(f"Plik do usunięcia nie istnieje: {target_path}")

        return {
            "deleted_count": deleted_count,
            "freed_bytes": freed_bytes,
            "freed_str": self._format_bytes(freed_bytes),
            "errors": errors
        }


duplicate_scanner = DuplicateScanner(settings.MUSIC_DIR)
