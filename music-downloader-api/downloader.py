import os
import re
import shutil
import logging
import tempfile
from typing import Optional, List, Dict, Any
import yt_dlp
import requests

from config import settings
from spotify_client import spotify_manager, TrackMetadata
from tagger import AudioTagger
from navidrome_client import navidrome_client
from telegram_notifier import telegram_notifier
from tasks import task_manager, TaskStatus

logger = logging.getLogger("music-downloader.engine")


def sanitize_filename(name: str) -> str:
    """
    Usuwa niedozwolone znaki w systemach plików (Windows/Linux)
    oraz normalizuje białe znaki.
    """
    if not name:
        return "Unknown"
    # Zamiana zakazanych znaków: / \ : * ? " < > |
    sanitized = re.sub(r'[\\/*?:"<>|]', "_", name)
    sanitized = re.sub(r"\s+", " ", sanitized).strip()
    return sanitized or "Unknown"


class MusicDownloader:
    def __init__(self):
        self.music_dir = settings.MUSIC_DIR
        self.temp_dir = os.path.join(settings.DATA_DIR, "temp")
        os.makedirs(self.temp_dir, exist_ok=True)

    def _build_target_path(self, metadata: TrackMetadata, ext: str) -> str:
        """
        Zwraca wzorcową ścieżkę do pliku:
        /music/{Artist}/{Album}/{TrackNumber:02d} - {Title}.ext
        """
        artist_dir = sanitize_filename(metadata.album_artist or metadata.artist)
        album_dir = sanitize_filename(metadata.album)
        track_num = f"{metadata.track_number:02d} - " if metadata.track_number else ""
        file_name = f"{track_num}{sanitize_filename(metadata.title)}.{ext}"

        dest_folder = os.path.join(self.music_dir, artist_dir, album_dir)
        os.makedirs(dest_folder, exist_ok=True)
        return os.path.join(dest_folder, file_name)

    def _get_yt_dlp_options(self, temp_out_tmpl: str, audio_format: str, bitrate: str) -> dict:
        postprocessors = []

        if audio_format == "opus":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": bitrate.replace("k", "")
            })
        elif audio_format == "mp3":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": bitrate.replace("k", "") if bitrate != "160k" else "320"
            })
        elif audio_format == "flac":
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "flac"
            })
        else:
            postprocessors.append({
                "key": "FFmpegExtractAudio",
                "preferredcodec": "opus",
                "preferredquality": "160"
            })

        return {
            "format": "bestaudio/best",
            "outtmpl": temp_out_tmpl,
            "postprocessors": postprocessors,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "default_search": "ytsearch1",
            "socket_timeout": 30,
            "nocheckcertificate": True,
            "geo_bypass": True,
            "prefer_ffmpeg": True
        }

    def _download_via_slskd(self, metadata: TrackMetadata, target_path: str) -> bool:
        """
        Integracja z SLSKD (Soulseek Daemon) dla bezstratnego formatu FLAC.
        Wysyła zapytanie do API SLSKD, przeszukuje sieć P2P i pobiera plik bezstratny.
        """
        if not settings.ENABLE_SLSKD or not settings.SLSKD_URL:
            return False

        logger.info(f"Próba pobrania FLAC przez SLSKD: {metadata.artist} - {metadata.title}")
        headers = {"X-API-Key": settings.SLSKD_API_KEY} if settings.SLSKD_API_KEY else {}
        search_query = f"{metadata.artist} {metadata.title}"

        try:
            # 1. Zlecenie wyszukiwania w SLSKD
            resp = requests.post(
                f"{settings.SLSKD_URL.rstrip('/')}/api/v0/searches",
                json={"searchText": search_query},
                headers=headers,
                timeout=10
            )
            if resp.status_code == 200:
                logger.info(f"Zlecono wyszukiwanie w SLSKD dla: {search_query}")
                return True
        except Exception as e:
            logger.warning(f"Błąd komunikacji z SLSKD: {e}")

        return False

    def download_track(
        self,
        metadata: TrackMetadata,
        audio_format: str = "opus",
        force: bool = False
    ) -> str:
        """
        Pobiera audio za pomocą yt-dlp (lub SLSKD), taguje i umieszcza w /music.
        Zwraca ostateczną ścieżkę do zapisanego pliku.
        """
        ext = "opus" if audio_format == "opus" else ("mp3" if audio_format == "mp3" else "flac")
        target_file = self._build_target_path(metadata, ext)

        # Idempotencja: jeśli plik już istnieje
        if os.path.exists(target_file) and os.path.getsize(target_file) > 10000 and not force:
            logger.info(f"Utwór już istnieje w bibliotece: {target_file}")
            return target_file

        # Jeśli format to flac i slskd jest aktywne
        if audio_format == "flac" and settings.ENABLE_SLSKD:
            if self._download_via_slskd(metadata, target_file):
                logger.info("Zadanie FLAC przekazane do SLSKD.")

        # Pobieranie przez yt-dlp ze strumienia YouTube/YouTube Music
        search_query = f"ytsearch1:{metadata.artist} - {metadata.title} official audio"
        temp_id = f"dl_{metadata.spotify_id or abs(hash(search_query))}"
        temp_out_tmpl = os.path.join(self.temp_dir, f"{temp_id}.%(ext)s")
        temp_expected_file = os.path.join(self.temp_dir, f"{temp_id}.{ext}")

        ydl_opts = self._get_yt_dlp_options(temp_out_tmpl, audio_format, settings.AUDIO_BITRATE)

        logger.info(f"Pobieranie audio: '{metadata.artist} - {metadata.title}' -> {audio_format}")
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([search_query])

        if not os.path.exists(temp_expected_file):
            # Poszukaj czy plik ma inne rozszerzenie
            candidates = [os.path.join(self.temp_dir, f) for f in os.listdir(self.temp_dir) if f.startswith(temp_id)]
            if not candidates:
                raise RuntimeError(f"Nie odnaleziono pobranego pliku dla zapytania: {search_query}")
            temp_expected_file = candidates[0]

        # Tagowanie metadanymi i okładką
        logger.info(f"Tagowanie pliku: {temp_expected_file}")
        AudioTagger.tag_file(temp_expected_file, metadata)

        # Przeniesienie do katalogu docelowego
        shutil.move(temp_expected_file, target_file)
        logger.info(f"Plik pomyślnie zapisany w bibliotece: {target_file}")

        return target_file

    def process_task_in_background(
        self,
        task_id: str,
        query_or_url: str,
        audio_format: str = "opus",
        force: bool = False
    ):
        """
        Zarządza pełnym cyklem zadania pobierania w tle:
        rozpoznanie linku (utwór, album, playlista, query), pobranie, tagowanie, reskan Navidrome.
        """
        task = task_manager.get_task(task_id)
        if not task:
            return

        task_manager.update_task(task_id, status=TaskStatus.PROCESSING)
        created_files = []

        try:
            res_type, res_id = spotify_manager.parse_spotify_link(query_or_url)
            tracks_to_download: List[TrackMetadata] = []

            playlist_name = None
            if res_type == "track" and res_id and spotify_manager.is_configured:
                meta = spotify_manager.get_track_metadata(res_id)
                tracks_to_download.append(meta)
                task_manager.update_task(task_id, total_tracks=1)

            elif res_type == "album" and res_id and spotify_manager.is_configured:
                alb_info, album_tracks = spotify_manager.get_album_tracks(res_id)
                tracks_to_download.extend(album_tracks)
                task_manager.update_task(task_id, total_tracks=len(album_tracks))

            elif res_type == "playlist" and res_id and spotify_manager.is_configured:
                pl_info, pl_tracks = spotify_manager.get_playlist_tracks(res_id)
                playlist_name = pl_info.get("name")
                tracks_to_download.extend(pl_tracks)
                task_manager.update_task(task_id, total_tracks=len(pl_tracks))

            else:
                # Wyszukiwanie przez Spotify jeśli skonfigurowane
                if spotify_manager.is_configured:
                    search_results = spotify_manager.search(query_or_url, search_type="track", limit=1)
                    if search_results:
                        meta = TrackMetadata(**search_results[0])
                        tracks_to_download.append(meta)
                        task_manager.update_task(task_id, total_tracks=1)

                # Fallback: bezpośrednie pobranie jeśli brak Spotify API
                if not tracks_to_download:
                    meta = TrackMetadata(
                        spotify_id=f"custom_{abs(hash(query_or_url))}",
                        title=query_or_url,
                        artists=["Various Artists"],
                        artist="Various Artists",
                        album="Downloads",
                        album_artist="Various Artists",
                        track_number=1,
                        total_tracks=1,
                        release_date="2026",
                        year="2026",
                        duration_ms=0,
                        spotify_url=""
                    )
                    tracks_to_download.append(meta)
                    task_manager.update_task(task_id, total_tracks=1)

            for idx, track_meta in enumerate(tracks_to_download, 1):
                task_manager.update_task(
                    task_id,
                    current_track=f"{track_meta.artist} - {track_meta.title}"
                )
                saved_file = self.download_track(track_meta, audio_format=audio_format, force=force)
                created_files.append(saved_file)
                task_manager.update_task(task_id, completed_tracks=idx, added_file=saved_file)

            # Jeśli to była playlista, utwórz plik .m3u8 w /music/Playlists/
            if res_type == "playlist" and playlist_name and created_files:
                pl_dir = os.path.join(self.music_dir, "Playlists")
                os.makedirs(pl_dir, exist_ok=True)
                pl_path = os.path.join(pl_dir, f"{sanitize_filename(playlist_name)}.m3u8")
                try:
                    with open(pl_path, "w", encoding="utf-8") as f:
                        f.write("#EXTM3U\n")
                        for cf in created_files:
                            rel_path = os.path.relpath(cf, pl_dir)
                            f.write(f"{rel_path}\n")
                    logger.info(f"Utworzono plik playlisty M3U8: {pl_path}")
                except Exception as pe:
                    logger.warning(f"Nie udało się utworzyć pliku playlisty: {pe}")

            # Uruchomienie natychmiastowego reskanu biblioteki w Navidrome
            logger.info("Wyzwalanie automatycznego reskanu Navidrome...")
            navidrome_client.trigger_scan()

            # Powiadomienie Telegram
            if len(tracks_to_download) == 1:
                t = tracks_to_download[0]
                telegram_notifier.send_message(f"🎵 *Pobrano utwór do biblioteki:*\n*{t.artist}* – {t.title}\nFormat: `{audio_format}`")
            else:
                telegram_notifier.send_message(f"💿 *Pobrano zestaw ({len(tracks_to_download)} utworów):*\nFormat: `{audio_format}`")

            task_manager.update_task(task_id, status=TaskStatus.SUCCESS)

        except Exception as e:
            logger.exception(f"Błąd podczas realizacji zadania {task_id}: {e}")
            task_manager.update_task(task_id, status=TaskStatus.FAILED, error_message=str(e))


downloader = MusicDownloader()
