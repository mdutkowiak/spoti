import re
import logging
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel
import spotipy
from spotipy.oauth2 import SpotifyClientCredentials
from config import settings

logger = logging.getLogger("music-downloader.spotify")


class TrackMetadata(BaseModel):
    spotify_id: str
    title: str
    artists: List[str]
    artist: str
    album: str
    album_artist: str
    track_number: int
    total_tracks: int
    disc_number: int = 1
    release_date: str
    year: str
    duration_ms: int
    cover_url: Optional[str] = None
    isrc: Optional[str] = None
    spotify_url: str


class SpotifyManager:
    def __init__(self):
        self.sp: Optional[spotipy.Spotify] = None
        self._init_client()

    def _init_client(self):
        if settings.SPOTIFY_CLIENT_ID and settings.SPOTIFY_CLIENT_SECRET:
            try:
                auth_manager = SpotifyClientCredentials(
                    client_id=settings.SPOTIFY_CLIENT_ID,
                    client_secret=settings.SPOTIFY_CLIENT_SECRET
                )
                self.sp = spotipy.Spotify(auth_manager=auth_manager)
                logger.info("Pomyślnie zainicjalizowano klienta Spotify Web API (Client Credentials).")
            except Exception as e:
                logger.error(f"Błąd inicjalizacji Spotify Web API: {e}")
                self.sp = None
        else:
            logger.warning("Brak kluczy SPOTIFY_CLIENT_ID i SPOTIFY_CLIENT_SECRET. Tryb Spotify API jest nieaktywny.")

    @property
    def is_configured(self) -> bool:
        return self.sp is not None

    @staticmethod
    def parse_spotify_link(url_or_query: str) -> Tuple[Optional[str], Optional[str]]:
        """
        Zwraca parę (resource_type, resource_id) dla linków spotify lub (None, None) dla zapytania tekstowego.
        Obsługuje:
        - https://open.spotify.com/track/ID?si=...
        - https://open.spotify.com/album/ID
        - https://open.spotify.com/playlist/ID
        - spotify:track:ID
        """
        if not url_or_query:
            return None, None

        # Format URL www
        web_match = re.search(r"open\.spotify\.com/(track|album|playlist)/([a-zA-Z0-9]+)", url_or_query)
        if web_match:
            return web_match.group(1), web_match.group(2)

        # Format URI
        uri_match = re.search(r"spotify:(track|album|playlist):([a-zA-Z0-9]+)", url_or_query)
        if uri_match:
            return uri_match.group(1), uri_match.group(2)

        return None, None

    def _format_track_item(self, item: Dict[str, Any], album_data: Optional[Dict[str, Any]] = None) -> TrackMetadata:
        album = album_data or item.get("album", {})
        artists_list = [a.get("name", "Unknown") for a in item.get("artists", [])]
        main_artist = artists_list[0] if artists_list else "Unknown Artist"
        artists_str = ", ".join(artists_list)
        
        album_artists = [a.get("name", "Unknown") for a in album.get("artists", [])]
        album_artist_str = album_artists[0] if album_artists else main_artist

        # Najlepsza okładka (zwykle pierwsza na liście jest w 640x640)
        images = album.get("images", [])
        cover_url = images[0].get("url") if images else None

        release_date = album.get("release_date", "1970")
        year = release_date.split("-")[0] if release_date else "1970"

        external_ids = item.get("external_ids", {})
        isrc = external_ids.get("isrc")

        external_urls = item.get("external_urls", {})
        spotify_url = external_urls.get("spotify", f"https://open.spotify.com/track/{item.get('id')}")

        return TrackMetadata(
            spotify_id=item.get("id", ""),
            title=item.get("name", "Unknown Title"),
            artists=artists_list,
            artist=artists_str,
            album=album.get("name", "Unknown Album"),
            album_artist=album_artist_str,
            track_number=item.get("track_number", 1),
            total_tracks=album.get("total_tracks", item.get("track_number", 1)),
            disc_number=item.get("disc_number", 1),
            release_date=release_date,
            year=year,
            duration_ms=item.get("duration_ms", 0),
            cover_url=cover_url,
            isrc=isrc,
            spotify_url=spotify_url
        )

    def get_track_metadata(self, track_id: str) -> TrackMetadata:
        if not self.sp:
            raise RuntimeError("Spotify API nie jest skonfigurowane (brak SPOTIFY_CLIENT_ID / SECRET).")
        track_data = self.sp.track(track_id)
        return self._format_track_item(track_data)

    def get_album_tracks(self, album_id: str) -> Tuple[Dict[str, Any], List[TrackMetadata]]:
        if not self.sp:
            raise RuntimeError("Spotify API nie jest skonfigurowane.")
        
        album = self.sp.album(album_id)
        tracks_res = self.sp.album_tracks(album_id, limit=50)
        items = tracks_res.get("items", [])
        
        # Paginacja dla albumów z >50 utworami
        while tracks_res.get("next"):
            tracks_res = self.sp.next(tracks_res)
            items.extend(tracks_res.get("items", []))

        result_tracks = [self._format_track_item(it, album_data=album) for it in items]
        album_info = {
            "id": album.get("id"),
            "name": album.get("name"),
            "artists": [a.get("name") for a in album.get("artists", [])],
            "release_date": album.get("release_date"),
            "total_tracks": album.get("total_tracks", len(result_tracks)),
            "cover_url": album.get("images", [{}])[0].get("url") if album.get("images") else None
        }
        return album_info, result_tracks

    def get_playlist_tracks(self, playlist_id: str) -> Tuple[Dict[str, Any], List[TrackMetadata]]:
        if not self.sp:
            raise RuntimeError("Spotify API nie jest skonfigurowane.")
        
        pl = self.sp.playlist(playlist_id)
        tracks_res = pl.get("tracks", {})
        items = tracks_res.get("items", [])
        
        while tracks_res.get("next"):
            tracks_res = self.sp.next(tracks_res)
            items.extend(tracks_res.get("items", []))

        result_tracks = []
        for item_wrapper in items:
            track = item_wrapper.get("track")
            if track and track.get("id") and not track.get("is_local"):
                result_tracks.append(self._format_track_item(track))

        pl_info = {
            "id": pl.get("id"),
            "name": pl.get("name"),
            "owner": pl.get("owner", {}).get("display_name"),
            "total_tracks": len(result_tracks),
            "cover_url": pl.get("images", [{}])[0].get("url") if pl.get("images") else None
        }
        return pl_info, result_tracks

    def search(self, query: str, search_type: str = "track", limit: int = 10) -> List[Dict[str, Any]]:
        if not self.sp:
            raise RuntimeError("Spotify API nie jest skonfigurowane.")
        
        search_res = self.sp.search(q=query, type=search_type, limit=limit)
        results = []

        if search_type == "track":
            tracks = search_res.get("tracks", {}).get("items", [])
            for t in tracks:
                results.append(self._format_track_item(t).model_dump())
        elif search_type == "album":
            albums = search_res.get("albums", {}).get("items", [])
            for a in albums:
                results.append({
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "artists": [art.get("name") for art in a.get("artists", [])],
                    "release_date": a.get("release_date"),
                    "total_tracks": a.get("total_tracks"),
                    "cover_url": a.get("images", [{}])[0].get("url") if a.get("images") else None,
                    "spotify_url": a.get("external_urls", {}).get("spotify")
                })
        elif search_type == "playlist":
            playlists = search_res.get("playlists", {}).get("items", [])
            for p in playlists:
                if p:
                    results.append({
                        "id": p.get("id"),
                        "name": p.get("name"),
                        "owner": p.get("owner", {}).get("display_name"),
                        "total_tracks": p.get("tracks", {}).get("total", 0),
                        "cover_url": p.get("images", [{}])[0].get("url") if p.get("images") else None,
                        "spotify_url": p.get("external_urls", {}).get("spotify")
                    })
        return results


spotify_manager = SpotifyManager()
