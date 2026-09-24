import base64
import logging
import os
from typing import Optional
import requests
from mutagen.oggopus import OggOpus
from mutagen.flac import FLAC, Picture
from mutagen.id3 import ID3, TIT2, TPE1, TALB, TPE2, TRCK, TPOS, TDRC, APIC, TSRC, ID3NoHeaderError
from mutagen.easyid3 import EasyID3
from spotify_client import TrackMetadata

logger = logging.getLogger("music-downloader.tagger")


class AudioTagger:
    @staticmethod
    def _download_image(cover_url: str) -> Optional[bytes]:
        try:
            resp = requests.get(cover_url, timeout=15)
            if resp.status_code == 200:
                return resp.content
        except Exception as e:
            logger.warning(f"Nie udało się pobrać okładki z {cover_url}: {e}")
        return None

    @classmethod
    def tag_file(cls, file_path: str, metadata: TrackMetadata):
        """
        Zapisuje perfekcyjne metadane ID3 / Vorbis Comments oraz okładkę albumu
        w zależności od formatu pliku (.opus, .flac, .mp3).
        """
        ext = os.path.splitext(file_path)[1].lower()
        image_data = cls._download_image(metadata.cover_url) if metadata.cover_url else None

        if ext == ".opus":
            cls._tag_opus(file_path, metadata, image_data)
        elif ext == ".flac":
            cls._tag_flac(file_path, metadata, image_data)
        elif ext in [".mp3", ".m4a"]:
            cls._tag_mp3(file_path, metadata, image_data)
        else:
            logger.warning(f"Nieobsługiwany format do tagowania: {ext}")

    @staticmethod
    def _tag_opus(file_path: str, metadata: TrackMetadata, image_data: Optional[bytes]):
        try:
            audio = OggOpus(file_path)
            
            # Tagi Vorbis
            audio["title"] = [metadata.title]
            audio["artist"] = [metadata.artist]
            audio["album"] = [metadata.album]
            audio["albumartist"] = [metadata.album_artist]
            audio["tracknumber"] = [str(metadata.track_number)]
            audio["tracktotal"] = [str(metadata.total_tracks)]
            audio["discnumber"] = [str(metadata.disc_number)]
            audio["date"] = [metadata.release_date]
            audio["year"] = [metadata.year]
            if metadata.isrc:
                audio["isrc"] = [metadata.isrc]

            # Osadzenie okładki zgodnie ze specyfikacją METADATA_BLOCK_PICTURE
            if image_data:
                pic = Picture()
                pic.data = image_data
                pic.type = 3  # Okładka przednia (front cover)
                pic.mime = "image/jpeg"
                pic.desc = "Cover"
                # Ogg Opus wymaga base64 wygenerowanego bloku FLAC Picture
                encoded_pic = base64.b64encode(pic.write()).decode("ascii")
                audio["metadata_block_picture"] = [encoded_pic]

            audio.save()
            logger.info(f"Otagowano plik Opus: {file_path}")
        except Exception as e:
            logger.error(f"Błąd podczas tagowania pliku Opus ({file_path}): {e}")

    @staticmethod
    def _tag_flac(file_path: str, metadata: TrackMetadata, image_data: Optional[bytes]):
        try:
            audio = FLAC(file_path)
            audio["title"] = metadata.title
            audio["artist"] = metadata.artist
            audio["album"] = metadata.album
            audio["albumartist"] = metadata.album_artist
            audio["tracknumber"] = str(metadata.track_number)
            audio["totaltracks"] = str(metadata.total_tracks)
            audio["discnumber"] = str(metadata.disc_number)
            audio["date"] = metadata.release_date
            if metadata.isrc:
                audio["isrc"] = metadata.isrc

            if image_data:
                audio.clear_pictures()
                pic = Picture()
                pic.data = image_data
                pic.type = 3
                pic.mime = "image/jpeg"
                audio.add_picture(pic)

            audio.save()
            logger.info(f"Otagowano plik FLAC: {file_path}")
        except Exception as e:
            logger.error(f"Błąd podczas tagowania pliku FLAC ({file_path}): {e}")

    @staticmethod
    def _tag_mp3(file_path: str, metadata: TrackMetadata, image_data: Optional[bytes]):
        try:
            try:
                tags = ID3(file_path)
            except ID3NoHeaderError:
                tags = ID3()

            tags.add(TIT2(encoding=3, text=metadata.title))
            tags.add(TPE1(encoding=3, text=metadata.artist))
            tags.add(TALB(encoding=3, text=metadata.album))
            tags.add(TPE2(encoding=3, text=metadata.album_artist))
            tags.add(TRCK(encoding=3, text=f"{metadata.track_number}/{metadata.total_tracks}"))
            tags.add(TPOS(encoding=3, text=str(metadata.disc_number)))
            tags.add(TDRC(encoding=3, text=metadata.year))
            if metadata.isrc:
                tags.add(TSRC(encoding=3, text=metadata.isrc))

            if image_data:
                tags.add(APIC(
                    encoding=3,
                    mime="image/jpeg",
                    type=3,
                    desc="Cover",
                    data=image_data
                ))

            tags.save(file_path, v2_version=3)
            logger.info(f"Otagowano plik MP3: {file_path}")
        except Exception as e:
            logger.error(f"Błąd podczas tagowania pliku MP3 ({file_path}): {e}")
