import re
import json
import logging
from typing import List, Optional, Tuple, Dict, Any
from pydantic import BaseModel
import requests
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

    def _extract_entity_from_embed(self, res_type: str, res_id: str) -> Dict[str, Any]:
        """
        Pobiera publiczne metadane ze Spotify Embed (omija ograniczenia API dla playlist w trybie Client Credentials).
        """
        url = f"https://open.spotify.com/embed/{res_type}/{res_id}"
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            "Accept-Language": "pl,en-US;q=0.9,en;q=0.8"
        }
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()

        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', resp.text, re.DOTALL)
        if not match:
            raise ValueError(f"Nie odnaleziono danych __NEXT_DATA__ na stronie Spotify Embed: {url}")

        data = json.loads(match.group(1))
        entity = (
            data.get("props", {})
            .get("pageProps", {})
            .get("state", {})
            .get("data", {})
            .get("entity", {})
        )
        if not entity:
            raise ValueError(f"Brak obiektu entity w danych Spotify Embed dla {url}")
        return entity

    def get_track_metadata(self, track_id: str) -> TrackMetadata:
        if self.sp:
            try:
                track_data = self.sp.track(track_id)
                return self._format_track_item(track_data)
            except Exception as e:
                logger.warning(f"Błąd pobierania utworu przez Spotify API ({e}), próba fallbacku...")

        entity = self._extract_entity_from_embed("track", track_id)
        title = entity.get("name") or entity.get("title") or "Unknown Title"
        artist = entity.get("subtitle") or "Unknown Artist"
        artists_list = [a.strip() for a in artist.split(",") if a.strip()] or [artist]
        sources = entity.get("coverArt", {}).get("sources", [])
        cover_url = sources[0].get("url") if sources else None
        duration = entity.get("duration", 0)

        return TrackMetadata(
            spotify_id=track_id,
            title=title,
            artists=artists_list,
            artist=artist,
            album=title,
            album_artist=artists_list[0],
            track_number=1,
            total_tracks=1,
            disc_number=1,
            release_date="2026",
            year="2026",
            duration_ms=duration,
            cover_url=cover_url,
            isrc=None,
            spotify_url=f"https://open.spotify.com/track/{track_id}"
        )

    def get_album_tracks(self, album_id: str) -> Tuple[Dict[str, Any], List[TrackMetadata]]:
        if self.sp:
            try:
                album = self.sp.album(album_id)
                tracks_res = self.sp.album_tracks(album_id, limit=50)
                items = tracks_res.get("items", [])
                
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
            except Exception as e:
                logger.warning(f"Błąd pobierania albumu przez Spotify API ({e}), próba fallbacku...")

        entity = self._extract_entity_from_embed("album", album_id)
        alb_name = entity.get("name") or entity.get("title") or "Album"
        sources = entity.get("coverArt", {}).get("sources", [])
        alb_cover = sources[0].get("url") if sources else None
        track_list = entity.get("trackList", [])

        result_tracks = []
        for idx, t in enumerate(track_list, 1):
            uri = t.get("uri", "")
            t_id = uri.split(":")[-1] if ":" in uri else uri
            subtitle = t.get("subtitle", "Unknown Artist")
            artists_list = [a.strip() for a in subtitle.split(",") if a.strip()] or ["Unknown Artist"]
            result_tracks.append(TrackMetadata(
                spotify_id=t_id or f"alb_{idx}",
                title=t.get("title", f"Track {idx}"),
                artists=artists_list,
                artist=subtitle,
                album=alb_name,
                album_artist=artists_list[0],
                track_number=idx,
                total_tracks=len(track_list),
                disc_number=1,
                release_date="2026",
                year="2026",
                duration_ms=t.get("duration", 0),
                cover_url=alb_cover,
                isrc=None,
                spotify_url=f"https://open.spotify.com/track/{t_id}" if t_id else ""
            ))

        alb_info = {
            "id": album_id,
            "name": alb_name,
            "artists": [entity.get("subtitle", "Unknown Artist")],
            "release_date": "2026",
            "total_tracks": len(result_tracks),
            "cover_url": alb_cover
        }
        return alb_info, result_tracks

    def get_playlist_tracks(self, playlist_id: str) -> Tuple[Dict[str, Any], List[TrackMetadata]]:
        result_tracks: List[TrackMetadata] = []
        pl_name = None
        pl_owner = None
        pl_cover = None

        # 1. Próba przez Spotipy API (jeśli dozwolone)
        if self.sp:
            try:
                tracks_res = self.sp.playlist_items(playlist_id, limit=50)
                items = tracks_res.get("items", []) if tracks_res else []
                while tracks_res and tracks_res.get("next"):
                    tracks_res = self.sp.next(tracks_res)
                    items.extend(tracks_res.get("items", []))

                for item_wrapper in items:
                    track = item_wrapper.get("track")
                    if track and track.get("id") and not track.get("is_local"):
                        result_tracks.append(self._format_track_item(track))

                if result_tracks:
                    pl = self.sp.playlist(playlist_id)
                    pl_name = pl.get("name")
                    pl_owner = pl.get("owner", {}).get("display_name")
                    pl_cover = pl.get("images", [{}])[0].get("url") if pl.get("images") else None
            except Exception as e:
                logger.info(f"Spotipy API nie zwróciło utworów playlisty ({e}), używam Spotify Embed Scraper...")
                result_tracks = []

        # 2. Bezpieczny fallback: Spotify Embed
        if not result_tracks:
            logger.info(f"Pobieranie playlisty {playlist_id} przez Spotify Embed...")
            entity = self._extract_entity_from_embed("playlist", playlist_id)
            pl_name = entity.get("name") or entity.get("title") or "Playlist"
            pl_owner = entity.get("subtitle") or "Spotify"
            sources = entity.get("coverArt", {}).get("sources", [])
            pl_cover = sources[0].get("url") if sources else None
            track_list = entity.get("trackList", [])

            # Zbierz ID utworów do wzbogacenia o oficjalne albumy, rok, ISRC itp. przez sp.tracks()
            track_ids = []
            for t in track_list:
                uri = t.get("uri", "")
                t_id = uri.split(":")[-1] if ":" in uri else uri
                if t_id:
                    track_ids.append(t_id)

            enriched_map: Dict[str, TrackMetadata] = {}
            if self.sp and track_ids:
                logger.info(f"Wzbogacanie metadanych dla {len(track_ids)} utworów przez Spotify Web API (sp.tracks)...")
                for i in range(0, len(track_ids), 50):
                    batch = track_ids[i:i + 50]
                    try:
                        resp = self.sp.tracks(batch)
                        for t_obj in resp.get("tracks", []):
                            if t_obj and t_obj.get("id"):
                                enriched_map[t_obj["id"]] = self._format_track_item(t_obj)
                    except Exception as be:
                        logger.warning(f"Błąd batch-pobierania metadanych Spotify ({be})")

            for idx, t in enumerate(track_list, 1):
                uri = t.get("uri", "")
                t_id = uri.split(":")[-1] if ":" in uri else uri
                if t_id and t_id in enriched_map:
                    result_tracks.append(enriched_map[t_id])
                else:
                    subtitle = t.get("subtitle", "Unknown Artist")
                    artists_list = [a.strip() for a in subtitle.split(",") if a.strip()] or ["Unknown Artist"]
                    result_tracks.append(TrackMetadata(
                        spotify_id=t_id or f"pl_{idx}",
                        title=t.get("title", f"Track {idx}"),
                        artists=artists_list,
                        artist=subtitle,
                        album=pl_name,
                        album_artist=artists_list[0],
                        track_number=idx,
                        total_tracks=len(track_list),
                        disc_number=1,
                        release_date="2026",
                        year="2026",
                        duration_ms=t.get("duration", 0),
                        cover_url=pl_cover,
                        isrc=None,
                        spotify_url=f"https://open.spotify.com/track/{t_id}" if t_id else ""
                    ))

        pl_info = {
            "id": playlist_id,
            "name": pl_name or "Playlist",
            "owner": pl_owner or "Spotify",
            "total_tracks": len(result_tracks),
            "cover_url": pl_cover
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
