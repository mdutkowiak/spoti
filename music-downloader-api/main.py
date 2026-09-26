import os
import shutil
import logging
import requests
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse, StreamingResponse, Response
from pydantic import BaseModel, Field

from config import settings
from spotify_client import spotify_manager, TrackMetadata, inspect_url
from downloader import downloader
from navidrome_client import navidrome_client
from tasks import task_manager, TaskInfo, TaskStatus
from library_checker import library_checker
from duplicate_scanner import duplicate_scanner

# Konfiguracja logowania
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
)
logger = logging.getLogger("music-downloader.api")

app = FastAPI(
    title="Self-Hosted Music Downloader API",
    description="Mikroserwis do pobierania on-demand ze Spotify i YouTube Music dla Navidrome / OpenSubsonic.",
    version="1.0.0"
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Weryfikacja klucza API (opcjonalna)
def verify_api_key(x_api_key: Optional[str] = Header(None), api_key: Optional[str] = Query(None)):
    if not settings.API_SECRET_KEY or settings.API_SECRET_KEY in ["", "change_this_secret_token_for_api_auth"]:
        return True
    key = x_api_key or api_key
    if settings.API_SECRET_KEY != key:
        raise HTTPException(status_code=401, detail="Nieprawidłowy lub brakujący klucz X-API-Key.")
    return True


# Modele zapytań i odpowiedzi
class DownloadRequest(BaseModel):
    query_or_url: str = Field(..., description="Link Spotify/YouTube lub zapytanie tekstowe 'Artysta - Tytuł'")
    format: Optional[str] = Field(default="opus", description="Format audio: opus (rekomendowany), mp3, flac")
    force: bool = Field(default=False, description="Czy wymusić ponowne pobranie, jeśli plik już istnieje")


class InspectRequest(BaseModel):
    url_or_query: str = Field(..., description="Link Spotify/YouTube lub zapytanie do inspekcji")


class DownloadSelectedRequest(BaseModel):
    tracks: List[TrackMetadata] = Field(..., description="Lista wybranych utworów z metadanymi")
    format: Optional[str] = Field(default="opus", description="Format audio: opus, mp3, flac")
    force: bool = Field(default=False, description="Czy wymusić ponowne pobranie")
    playlist_name: Optional[str] = Field(default=None, description="Opcjonalna nazwa playlisty m3u8")


class DownloadResponse(BaseModel):
    task_id: str
    status: str
    message: str


class RefreshResponse(BaseModel):
    status: str
    message: str
    details: Optional[dict] = None


class DeleteDuplicatesRequest(BaseModel):
    paths: List[str] = Field(..., description="Lista relatywnych ścieżek plików do usunięcia")


class DeleteTracksRequest(BaseModel):
    paths: List[str] = Field(..., description="Lista relatywnych ścieżek plików do usunięcia")


class AddTrackToPlaylistRequest(BaseModel):
    title: str = Field(..., description="Tytuł utworu")
    artist: Optional[str] = Field(default="", description="Wykonawca utworu")
    rel_path: Optional[str] = Field(default=None, description="Opcjonalna relatywna ścieżka pliku")


class UpdateTrackMetadataRequest(BaseModel):
    rel_path: str = Field(..., description="Ścieżka relatywna pliku w /music")
    title: str = Field(..., description="Nowy tytuł utworu")
    artist: str = Field(..., description="Nowy wykonawca")
    album: str = Field(..., description="Nowy album")


class ReplaceTrackRequest(BaseModel):
    old_rel_path: str = Field(..., description="Ścieżka relatywna starego pliku")
    new_query_or_url: str = Field(..., description="Link Spotify/YouTube lub tytuł nowej wersji")
    audio_format: Optional[str] = Field(default="opus", description="Format audio")
    custom_title: Optional[str] = Field(default=None)
    custom_artist: Optional[str] = Field(default=None)
    custom_album: Optional[str] = Field(default=None)


class CreatePlaylistRequest(BaseModel):
    name: str = Field(..., description="Nazwa nowej playlisty")
    song_ids: Optional[List[str]] = Field(default=None, description="Opcjonalne ID utworów do dodania")


class AddTracksToPlaylistRequest(BaseModel):
    song_ids: List[str] = Field(..., description="Lista ID utworów do dodania")


class CreateUserRequest(BaseModel):
    username: str = Field(..., description="Login użytkownika")
    name: Optional[str] = Field(default="", description="Imię / Nazwa wyświetlana")
    password: str = Field(..., description="Hasło")
    email: Optional[str] = Field(default="", description="Adres e-mail")
    is_admin: Optional[bool] = Field(default=False, description="Czy nadać uprawnienia administratora")


class UpdateUserRequest(BaseModel):
    username: Optional[str] = Field(default=None)
    name: Optional[str] = Field(default=None)
    password: Optional[str] = Field(default=None)
    email: Optional[str] = Field(default=None)
    is_admin: Optional[bool] = Field(default=None)


@app.get("/health", tags=["System"])
def health_check():
    return {
        "status": "healthy",
        "spotify_configured": spotify_manager.is_configured,
        "default_format": settings.DEFAULT_AUDIO_FORMAT,
        "music_dir": settings.MUSIC_DIR,
        "navidrome_url": settings.NAVIDROME_URL
    }


@app.get("/search", tags=["Spotify"])
def search_music(
    q: str = Query(..., description="Wyszukiwana fraza"),
    type: str = Query("track", regex="^(track|album|playlist)$"),
    limit: int = Query(10, ge=1, le=50),
    _: bool = Depends(verify_api_key)
):
    """
    Przeszukuje katalog Spotify w poszukiwaniu utworów, albumów lub playlist
    wraz z metadanymi i okładkami w wysokiej rozdzielczości.
    """
    try:
        results = spotify_manager.search(query=q, search_type=type, limit=limit)
        if type == "track":
            for item in results:
                item["in_library"] = library_checker.contains(item.get("artist", ""), item.get("title", ""))
        return {"query": q, "type": type, "count": len(results), "results": results}
    except Exception as e:
        logger.error(f"Błąd wyszukiwania: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/search/youtube", tags=["Downloader"])
def search_youtube_versions(
    q: str = Query(..., min_length=1, description="Szukana fraza na YouTube"),
    limit: int = Query(6, ge=1, le=20),
    _: bool = Depends(verify_api_key)
):
    """
    Wyszukuje alternatywne wersje utworu bezpośrednio na YouTube (oficjalne audio, teledyski, koncerty).
    """
    try:
        return {"query": q, "results": downloader.search_youtube_versions(q, limit=limit)}
    except Exception as e:
        logger.error(f"Błąd wyszukiwania YouTube: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/preview/audio", tags=["Downloader"])
def preview_audio_stream(
    request: Request,
    url: Optional[str] = Query(None, description="Link YouTube lub Spotify"),
    q: Optional[str] = Query(None, description="Tytuł / fraza do wyszukania audio"),
    _: bool = Depends(verify_api_key)
):
    """
    Strumieniuje audio z YouTube do przeglądarki z obsługą nagłówków Range (możliwość przewijania).
    """
    target = url or q
    if not target:
        raise HTTPException(status_code=400, detail="Brak parametru url lub q do odsłuchania.")

    stream_url = downloader.get_audio_stream_url(target)
    if not stream_url:
        raise HTTPException(status_code=404, detail="Nie udało się uzyskać strumienia audio dla tego utworu.")

    req_headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }
    range_header = request.headers.get("range")
    if range_header:
        req_headers["Range"] = range_header

    try:
        resp = requests.get(stream_url, headers=req_headers, stream=True, timeout=15)
        response_headers = {
            "Accept-Ranges": "bytes",
            "Content-Type": resp.headers.get("Content-Type", "audio/mp4"),
        }
        if "Content-Range" in resp.headers:
            response_headers["Content-Range"] = resp.headers["Content-Range"]
        if "Content-Length" in resp.headers:
            response_headers["Content-Length"] = resp.headers["Content-Length"]

        def stream_chunks():
            for chunk in resp.iter_content(chunk_size=64 * 1024):
                if chunk:
                    yield chunk

        return StreamingResponse(
            stream_chunks(),
            status_code=resp.status_code,
            headers=response_headers,
            media_type=response_headers["Content-Type"]
        )
    except Exception as e:
        logger.error(f"Błąd strumieniowania preview audio: {e}")
        raise HTTPException(status_code=500, detail=f"Błąd strumieniowania: {e}")


@app.post("/inspect", tags=["Downloader"])
def inspect_media_link(req: InspectRequest, _: bool = Depends(verify_api_key)):
    """
    Bada link ze Spotify (playlista, album, utwór) lub YouTube (playlista, film)
    i zwraca listę utworów z metadanymi do podglądu, selekcji i edycji.
    """
    try:
        data = inspect_url(req.url_or_query)
        for t in data.get("tracks", []):
            t["in_library"] = library_checker.contains(t.get("artist", ""), t.get("title", ""))
        return data
    except Exception as e:
        logger.error(f"Błąd inspekcji linku {req.url_or_query}: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/download", response_model=DownloadResponse, status_code=202, tags=["Downloader"])
def start_download_task(
    req: DownloadRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_api_key)
):
    """
    Zleca asynchroniczne pobranie utworu, albumu lub playlisty.
    Odpowiedź zwracana jest natychmiast z identyfikatorem task_id do śledzenia statusu.
    """
    audio_format = req.format or settings.DEFAULT_AUDIO_FORMAT
    task = task_manager.create_task(query=req.query_or_url)

    background_tasks.add_task(
        downloader.process_task_in_background,
        task_id=task.task_id,
        query_or_url=req.query_or_url,
        audio_format=audio_format,
        force=req.force
    )

    return DownloadResponse(
        task_id=task.task_id,
        status="accepted",
        message="Zadanie pobierania zostało zakolejkowane."
    )


@app.post("/download-selected", response_model=DownloadResponse, status_code=202, tags=["Downloader"])
def start_custom_download_task(
    req: DownloadSelectedRequest,
    background_tasks: BackgroundTasks,
    _: bool = Depends(verify_api_key)
):
    """
    Pobiera indywidualnie wyselekcjonowane przez użytkownika utwory z ew. edytowanymi metadanymi.
    """
    if not req.tracks:
        raise HTTPException(status_code=400, detail="Brak zaznaczonych utworów do pobrania.")

    audio_format = req.format or settings.DEFAULT_AUDIO_FORMAT
    query_desc = f"Wybrane utwory ({len(req.tracks)})"
    if req.playlist_name:
        query_desc += f" z {req.playlist_name}"

    task = task_manager.create_task(query=query_desc)
    background_tasks.add_task(
        downloader.process_custom_tracks_in_background,
        task_id=task.task_id,
        tracks=req.tracks,
        audio_format=audio_format,
        force=req.force,
        playlist_name=req.playlist_name
    )

    return DownloadResponse(
        task_id=task.task_id,
        status="accepted",
        message=f"Zakolejkowano pobieranie {len(req.tracks)} utworów."
    )


@app.post("/library/clear", tags=["Library"])
def clear_music_library(_: bool = Depends(verify_api_key)):
    """
    Całkowicie czyści zawartość biblioteki /music i wywołuje pełny głęboki reskan w Navidrome.
    """
    deleted_count = 0
    music_dir = settings.MUSIC_DIR
    if os.path.exists(music_dir):
        for item in os.listdir(music_dir):
            item_path = os.path.join(music_dir, item)
            try:
                if os.path.isfile(item_path) or os.path.islink(item_path):
                    os.unlink(item_path)
                    deleted_count += 1
                elif os.path.isdir(item_path):
                    shutil.rmtree(item_path)
                    deleted_count += 1
            except Exception as e:
                logger.error(f"Błąd usuwania {item_path}: {e}")

    library_checker.invalidate()
    duplicate_scanner.invalidate_cache()
    res = navidrome_client.trigger_scan(full_scan=True)
    return {
        "status": "success",
        "deleted_count": deleted_count,
        "message": f"Wyczyszczono bibliotekę muzyczną (usunięto {deleted_count} elementów).",
        "navidrome": res
    }


@app.get("/tasks/{task_id}", response_model=TaskInfo, tags=["Downloader"])
def get_task_status(task_id: str, _: bool = Depends(verify_api_key)):
    """
    Sprawdza stan bieżącego zadania pobierania (pending, processing, success, failed).
    """
    task = task_manager.get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Zadanie o podanym identyfikatorze nie istnieje.")
    return task


@app.get("/tasks", response_model=List[TaskInfo], tags=["Downloader"])
def list_recent_tasks(limit: int = 15, _: bool = Depends(verify_api_key)):
    return task_manager.list_tasks(limit=limit)


@app.post("/refresh-navidrome", response_model=RefreshResponse, tags=["Navidrome"])
def trigger_navidrome_rescan(
    full_scan: bool = Query(False, description="Wymuszenie pełnego głębokiego skanowania"),
    _: bool = Depends(verify_api_key)
):
    """
    Wywołuje natychmiastowe odświeżenie biblioteki w Navidrome przez Subsonic API.
    """
    res = navidrome_client.trigger_scan(full_scan=full_scan)
    return RefreshResponse(
        status=res.get("status", "unknown"),
        message="Żądanie odświeżenia wysłane do Navidrome.",
        details=res
    )


@app.post("/webhook/telegram", tags=["Integrations"])
async def telegram_webhook(update: dict, background_tasks: BackgroundTasks):
    """
    Odbiera webhooki z bota Telegram. Użytkownik wkleja link Spotify na czacie,
    a serwer natychmiast zleca pobranie i odświeżenie biblioteki.
    """
    message = update.get("message", {})
    text = message.get("text", "").strip()
    chat_id = message.get("chat", {}).get("id")

    if not text or not chat_id:
        return {"status": "ignored"}

    logger.info(f"Odebrano wiadomość z Telegram: '{text}' od chat_id: {chat_id}")
    task = task_manager.create_task(query=text)

    background_tasks.add_task(
        downloader.process_task_in_background,
        task_id=task.task_id,
        query_or_url=text,
        audio_format=settings.DEFAULT_AUDIO_FORMAT,
        force=False
    )

    return {"status": "queued", "task_id": task.task_id}


# =============================================================================
# LIBRARY MANAGEMENT ENDPOINTS
# =============================================================================

@app.get("/library/tracks", tags=["Library"])
def get_library_tracks(force: bool = False, _: bool = Depends(verify_api_key)):
    """
    Zwraca pełną listę wszystkich utworów w bibliotece wraz ze szczegółowymi statystykami.
    """
    return duplicate_scanner.get_all_tracks(force_refresh=force)


@app.delete("/library/tracks", tags=["Library"])
def delete_library_tracks(req: DeleteTracksRequest, _: bool = Depends(verify_api_key)):
    """
    Bezpiecznie usuwa wybrane pliki z biblioteki i odświeża indeksy w Navidrome.
    """
    res = duplicate_scanner.delete_files(req.paths)
    if res.get("deleted_count", 0) > 0:
        duplicate_scanner.invalidate_cache()
        library_checker.invalidate()
        navidrome_client.trigger_scan(full_scan=False)
    return res


@app.get("/library/stream", tags=["Library"])
def stream_audio(path: str = Query(...), _: bool = Depends(verify_api_key)):
    """
    Udostępnia plik audio do bezpośredniego odsłuchania / streamingu w przeglądarce.
    """
    music_dir_abs = os.path.abspath(settings.MUSIC_DIR)
    target_path = os.path.abspath(os.path.join(music_dir_abs, path.strip().lstrip("/\\")))
    if not target_path.startswith(music_dir_abs) or not os.path.isfile(target_path):
        raise HTTPException(status_code=404, detail="Plik nie istnieje na serwerze.")

    ext = os.path.splitext(target_path)[1].lower()
    media_types = {
        ".opus": "audio/ogg; codecs=opus",
        ".ogg": "audio/ogg",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".m4a": "audio/mp4",
        ".wav": "audio/wav"
    }
    return FileResponse(target_path, media_type=media_types.get(ext, "application/octet-stream"))


@app.get("/library/cover", tags=["Library"])
def get_library_cover(path: str = Query(...), _: bool = Depends(verify_api_key)):
    """
    Zwraca okładkę albumu z tagów pliku audio lub folderu.
    """
    img_bytes, mime = duplicate_scanner.get_track_cover(path)
    if img_bytes and mime:
        return Response(
            content=img_bytes,
            media_type=mime,
            headers={"Cache-Control": "public, max-age=86400"}
        )
    svg_placeholder = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100" width="100" height="100">
        <rect width="100" height="100" fill="#1e293b"/>
        <circle cx="50" cy="50" r="32" fill="#0f172a" stroke="#334155" stroke-width="3"/>
        <circle cx="50" cy="50" r="10" fill="#1db954"/>
        <path d="M50 35 v18 l12 -6 z" fill="#ffffff" opacity="0.9"/>
    </svg>"""
    return Response(content=svg_placeholder, media_type="image/svg+xml")


@app.put("/library/tracks/metadata", tags=["Library"])
def update_track_metadata(req: UpdateTrackMetadataRequest, _: bool = Depends(verify_api_key)):
    """
    Aktualizuje tagi (tytuł, wykonawca, album) w pliku audio i wywołuje odświeżenie Navidrome.
    """
    try:
        res = duplicate_scanner.update_track_metadata(req.rel_path, req.title, req.artist, req.album)
        library_checker.invalidate()
        navidrome_client.trigger_scan(full_scan=False)
        return {"status": "success", "message": "Zaktualizowano metadane utworu.", "track": res}
    except Exception as e:
        logger.error(f"Błąd aktualizacji metadanych: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/library/replace-track", tags=["Library"])
def replace_library_track(req: ReplaceTrackRequest, _: bool = Depends(verify_api_key)):
    """
    Pobiera wybraną wersję utworu, bezpiecznie podmienia plik na dysku i odświeża bazę.
    """
    try:
        new_path = downloader.replace_track(
            old_rel_path=req.old_rel_path,
            new_query_or_url=req.new_query_or_url,
            audio_format=req.audio_format or settings.DEFAULT_AUDIO_FORMAT,
            custom_title=req.custom_title,
            custom_artist=req.custom_artist,
            custom_album=req.custom_album
        )
        return {
            "status": "success",
            "message": "Pomyślnie podmieniono utwór na wybraną wersję.",
            "new_path": new_path
        }
    except Exception as e:
        logger.error(f"Błąd podmiany utworu: {e}")
        raise HTTPException(status_code=400, detail=str(e))


# =============================================================================
# DUPLICATE MANAGEMENT ENDPOINTS
# =============================================================================

@app.get("/library/duplicates", tags=["Duplicates"])
def get_library_duplicates(_: bool = Depends(verify_api_key)):
    """
    Skanuje całą bibliotekę i zwraca listę grup duplikatów po tytule utworu.
    """
    return duplicate_scanner.find_duplicates()


@app.post("/library/duplicates/delete", tags=["Duplicates"])
def delete_duplicate_files(req: DeleteDuplicatesRequest, _: bool = Depends(verify_api_key)):
    """
    Bezpiecznie usuwa wybrane pliki z dysku i wywołuje odświeżenie indeksu biblioteki.
    """
    res = duplicate_scanner.delete_files(req.paths)
    if res.get("deleted_count", 0) > 0:
        library_checker.invalidate()
        navidrome_client.trigger_scan(full_scan=False)
    return res


# =============================================================================
# PLAYLIST MANAGEMENT ENDPOINTS (SUBSONIC API)
# =============================================================================

@app.get("/library/playlists", tags=["Playlists"])
def get_playlists(_: bool = Depends(verify_api_key)):
    return navidrome_client.get_playlists()


@app.get("/library/playlists/{playlist_id}", tags=["Playlists"])
def get_playlist_details(playlist_id: str, _: bool = Depends(verify_api_key)):
    return navidrome_client.get_playlist(playlist_id)


@app.post("/library/playlists", tags=["Playlists"])
def create_playlist(req: CreatePlaylistRequest, _: bool = Depends(verify_api_key)):
    try:
        return navidrome_client.create_playlist(req.name, req.song_ids)
    except Exception as e:
        logger.error(f"Błąd tworzenia playlisty: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/library/playlists/{playlist_id}/tracks", tags=["Playlists"])
def add_tracks_to_playlist(playlist_id: str, req: AddTracksToPlaylistRequest, _: bool = Depends(verify_api_key)):
    try:
        return navidrome_client.add_tracks_to_playlist(playlist_id, req.song_ids)
    except Exception as e:
        logger.error(f"Błąd dodawania utworów do playlisty: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/library/playlists/{playlist_id}/tracks/{song_index}", tags=["Playlists"])
def remove_track_from_playlist(playlist_id: str, song_index: int, _: bool = Depends(verify_api_key)):
    try:
        return navidrome_client.remove_track_from_playlist(playlist_id, song_index)
    except Exception as e:
        logger.error(f"Błąd usuwania utworu z playlisty: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/library/playlists/{playlist_id}", tags=["Playlists"])
def delete_playlist(playlist_id: str, _: bool = Depends(verify_api_key)):
    success = navidrome_client.delete_playlist(playlist_id)
    if not success:
        raise HTTPException(status_code=400, detail="Nie udało się usunąć playlisty")
    return {"status": "success"}


@app.get("/library/songs/search", tags=["Playlists"])
def search_navidrome_songs(q: str = Query(..., min_length=1), _: bool = Depends(verify_api_key)):
    return navidrome_client.search_songs(q)


@app.post("/library/playlists/{playlist_id}/add-track", tags=["Playlists"])
def add_single_track_to_playlist(playlist_id: str, req: AddTrackToPlaylistRequest, _: bool = Depends(verify_api_key)):
    """
    Wyszukuje utwór w Navidrome po tytule i wykonawcy, a następnie dodaje go do wybranej playlisty.
    """
    query = f"{req.artist} {req.title}".strip() if req.artist else req.title
    songs = navidrome_client.search_songs(query, count=10)
    if not songs and req.title:
        songs = navidrome_client.search_songs(req.title, count=10)

    if not songs:
        raise HTTPException(
            status_code=404,
            detail="Nie znaleziono utworu w indeksie Navidrome. Upewnij się, że biblioteka została przeskanowana w Navidrome."
        )

    song_id = songs[0].get("id")
    if not song_id:
        raise HTTPException(status_code=404, detail="Brak identyfikatora utworu w Navidrome.")

    return navidrome_client.add_tracks_to_playlist(playlist_id, [song_id])


# =============================================================================
# USER MANAGEMENT ENDPOINTS (NAVIDROME NATIVE API)
# =============================================================================

@app.get("/library/users", tags=["Users"])
def get_navidrome_users(_: bool = Depends(verify_api_key)):
    return navidrome_client.get_users()


@app.post("/library/users", tags=["Users"])
def create_navidrome_user(req: CreateUserRequest, _: bool = Depends(verify_api_key)):
    try:
        return navidrome_client.create_user(
            username=req.username,
            name=req.name or req.username,
            password=req.password,
            email=req.email or "",
            is_admin=req.is_admin or False
        )
    except Exception as e:
        logger.error(f"Błąd tworzenia użytkownika: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.put("/library/users/{user_id}", tags=["Users"])
def update_navidrome_user(user_id: str, req: UpdateUserRequest, _: bool = Depends(verify_api_key)):
    try:
        return navidrome_client.update_user(
            user_id=user_id,
            username=req.username,
            name=req.name,
            password=req.password,
            email=req.email,
            is_admin=req.is_admin
        )
    except Exception as e:
        logger.error(f"Błąd aktualizacji użytkownika: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.delete("/library/users/{user_id}", tags=["Users"])
def delete_navidrome_user(user_id: str, _: bool = Depends(verify_api_key)):
    try:
        return {"status": "success", "deleted": navidrome_client.delete_user(user_id)}
    except Exception as e:
        logger.error(f"Błąd usuwania użytkownika: {e}")
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/", response_class=HTMLResponse, tags=["UI"])
def web_dashboard():
    """
    Wbudowany, responsywny web UI do wyszukiwania, wklejania linków i pobierania on-demand.
    """
    html_content = r"""<!DOCTYPE html>
<html lang="pl">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Music Downloader & Navidrome Hub</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Plus+Jakarta+Sans:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>
    :root {
      --bg: #0f172a;
      --card-bg: #1e293b;
      --accent: #1db954;
      --accent-hover: #1ed760;
      --text: #f8fafc;
      --text-muted: #94a3b8;
      --border: #334155;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: 'Plus Jakarta Sans', sans-serif;
      background-color: var(--bg);
      color: var(--text);
      display: flex;
      flex-direction: column;
      align-items: center;
      min-height: 100vh;
      padding: 2rem 1rem;
    }
    .container {
      width: 100%;
      max-width: 860px;
    }
    header {
      text-align: center;
      margin-bottom: 2rem;
    }
    header h1 {
      font-size: 2.2rem;
      font-weight: 700;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.6rem;
      color: var(--accent);
    }
    header p {
      color: var(--text-muted);
      margin-top: 0.5rem;
    }
    .card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 16px;
      padding: 1.6rem;
      margin-bottom: 1.2rem;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
    }
    .inspector-card {
      background: var(--card-bg);
      border: 1.5px solid var(--accent);
      border-radius: 16px;
      padding: 1.6rem;
      margin-bottom: 1.2rem;
      box-shadow: 0 12px 30px -5px rgba(29, 185, 84, 0.15);
    }
    .form-group {
      margin-bottom: 1.2rem;
    }
    label {
      display: block;
      font-size: 0.88rem;
      font-weight: 600;
      margin-bottom: 0.4rem;
      color: var(--text-muted);
    }
    input, select {
      width: 100%;
      padding: 0.85rem 1rem;
      border-radius: 10px;
      border: 1px solid var(--border);
      background: #0f172a;
      color: #fff;
      font-size: 1rem;
      outline: none;
      transition: border-color 0.2s;
    }
    input:focus, select:focus {
      border-color: var(--accent);
    }
    .btn-row {
      display: flex;
      gap: 0.8rem;
    }
    button {
      flex: 1;
      padding: 0.85rem 1.2rem;
      border-radius: 10px;
      font-weight: 600;
      font-size: 1rem;
      cursor: pointer;
      border: none;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
    }
    .btn-primary {
      background: var(--accent);
      color: #000;
    }
    .btn-primary:hover {
      background: var(--accent-hover);
      transform: translateY(-1px);
    }
    .btn-secondary {
      background: #334155;
      color: #fff;
    }
    .btn-secondary:hover {
      background: #475569;
    }
    .btn-danger-soft {
      background: #3b1e1e;
      border: 1px solid #7f1d1d;
      color: #fca5a5;
    }
    .btn-danger-soft:hover {
      background: #4c1d1d;
      border-color: #991b1b;
      color: #fecaca;
    }
    .status-box {
      margin-top: 1rem;
      padding: 1rem;
      border-radius: 10px;
      background: #0f172a;
      border: 1px solid var(--border);
      display: none;
    }
    .results-grid {
      display: grid;
      grid-template-columns: 1fr;
      gap: 0.8rem;
      margin-top: 1rem;
    }
    .track-item {
      display: flex;
      align-items: center;
      gap: 1rem;
      padding: 0.8rem;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 10px;
    }
    .track-item img {
      width: 52px;
      height: 52px;
      border-radius: 6px;
      object-fit: cover;
    }
    .track-info {
      flex: 1;
    }
    .track-title {
      font-weight: 600;
      font-size: 0.95rem;
    }
    .track-artist {
      font-size: 0.82rem;
      color: var(--text-muted);
    }
    .download-small-btn {
      padding: 0.5rem 0.9rem;
      background: var(--accent);
      color: #000;
      font-size: 0.85rem;
      border-radius: 8px;
      border: none;
      cursor: pointer;
      font-weight: 600;
      white-space: nowrap;
    }
    .download-small-btn:hover {
      background: var(--accent-hover);
    }
    .badge {
      display: inline-block;
      padding: 0.2rem 0.55rem;
      border-radius: 20px;
      font-size: 0.72rem;
      font-weight: 700;
      background: rgba(29, 185, 84, 0.15);
      color: var(--accent);
      border: 1px solid rgba(29, 185, 84, 0.3);
      letter-spacing: 0.5px;
    }
    .track-row {
      display: flex;
      gap: 0.8rem;
      align-items: center;
      background: #0f172a;
      padding: 0.7rem 0.9rem;
      border-radius: 10px;
      border: 1px solid var(--border);
    }
    .track-row:hover {
      border-color: #475569;
    }
    .track-inputs {
      flex: 1;
      display: grid;
      grid-template-columns: 2fr 1.5fr 1.5fr 70px;
      gap: 0.4rem;
      align-items: center;
    }
    @media (max-width: 720px) {
      .track-inputs {
        grid-template-columns: 1fr;
      }
      .track-row {
        flex-direction: column;
        align-items: stretch;
      }
    }
    .field-lbl {
      font-size: 0.7rem;
      color: var(--text-muted);
      margin-bottom: 2px;
      display: block;
    }
    .input-compact {
      width: 100%;
      padding: 0.4rem 0.6rem;
      border-radius: 6px;
      border: 1px solid var(--border);
      background: #1e293b;
      color: #fff;
      font-size: 0.85rem;
      outline: none;
    }
    .input-compact:focus {
      border-color: var(--accent);
    }
    .versions-panel {
      background: #0b1120;
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 0.8rem;
      margin-top: 0.5rem;
    }
    .version-item {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 0.8rem;
      padding: 0.5rem 0.6rem;
      border-radius: 6px;
      background: #1e293b;
      margin-bottom: 0.4rem;
      border: 1px solid #334155;
    }
    .version-item:last-child {
      margin-bottom: 0;
    }
    .version-item img {
      width: 44px;
      height: 44px;
      border-radius: 4px;
      object-fit: cover;
      flex-shrink: 0;
    }
    .version-item .download-small-btn {
      flex: 0 0 auto !important;
      width: max-content !important;
      margin-left: auto !important;
      padding: 0.45rem 0.85rem !important;
      font-size: 0.82rem !important;
      white-space: nowrap !important;
    }
    .badge-in-library {
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      color: #ef4444;
      font-weight: 700;
      font-size: 0.78rem;
      background: rgba(239, 68, 68, 0.15);
      padding: 3px 8px;
      border-radius: 6px;
      border: 1px solid rgba(239, 68, 68, 0.4);
      white-space: nowrap;
    }
    .badge-duration {
      display: inline-flex;
      align-items: center;
      gap: 0.25rem;
      font-size: 0.8rem;
      color: #38bdf8;
      font-weight: 600;
      background: rgba(56, 189, 248, 0.12);
      padding: 3px 7px;
      border-radius: 6px;
      border: 1px solid rgba(56, 189, 248, 0.25);
      white-space: nowrap;
    }
    .btn-search-version {
      background: #334155;
      color: #f8fafc;
      border: 1px solid #475569;
      padding: 0.45rem 0.8rem;
      border-radius: 6px;
      font-size: 0.82rem;
      font-weight: 600;
      cursor: pointer;
      transition: all 0.2s;
      white-space: nowrap;
    }
    .btn-search-version:hover {
      background: #475569;
      border-color: #64748b;
    }
    .toast {
      position: fixed;
      bottom: 24px;
      right: 24px;
      background: #1e293b;
      border: 1px solid var(--accent);
      color: #fff;
      padding: 0.8rem 1.2rem;
      border-radius: 8px;
      box-shadow: 0 10px 25px rgba(0,0,0,0.5);
      z-index: 9999;
      font-size: 0.9rem;
      display: flex;
      align-items: center;
      gap: 0.6rem;
      animation: toastSlideIn 0.3s ease-out;
      max-width: 420px;
    }
    @keyframes toastSlideIn {
      from { transform: translateY(30px); opacity: 0; }
      to { transform: translateY(0); opacity: 1; }
    }
    .nav-links {
      display: flex;
      justify-content: center;
      gap: 1.5rem;
      margin-top: 1rem;
    }
    .nav-links a {
      color: var(--accent);
      text-decoration: none;
      font-size: 0.9rem;
      font-weight: 500;
    }
    .nav-links a:hover {
      text-decoration: underline;
    }

    /* TABS */
    .tabs-nav {
      display: flex;
      gap: 0.5rem;
      margin-bottom: 1.5rem;
      background: #0f172a;
      padding: 0.4rem;
      border-radius: 12px;
      border: 1px solid var(--border);
      flex-wrap: wrap;
    }
    .tab-btn {
      flex: 1;
      min-width: 140px;
      padding: 0.75rem 1rem;
      border-radius: 8px;
      background: transparent;
      color: var(--text-muted);
      font-size: 0.9rem;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: all 0.2s ease;
      display: flex;
      align-items: center;
      justify-content: center;
      gap: 0.5rem;
    }
    .tab-btn:hover {
      color: #fff;
      background: rgba(255, 255, 255, 0.05);
    }
    .tab-btn.active {
      background: var(--accent);
      color: #000;
      box-shadow: 0 4px 12px rgba(29, 185, 84, 0.25);
    }
    .tab-content {
      display: none;
      animation: tabFadeIn 0.2s ease-out;
    }
    .tab-content.active {
      display: block;
    }
    @keyframes tabFadeIn {
      from { opacity: 0; transform: translateY(5px); }
      to { opacity: 1; transform: translateY(0); }
    }
    .stats-card {
      display: flex;
      gap: 1rem;
      flex-wrap: wrap;
      background: #0f172a;
      padding: 1rem 1.2rem;
      border-radius: 10px;
      border: 1px solid var(--border);
      margin-bottom: 1.2rem;
    }
    .stats-item {
      flex: 1;
      min-width: 140px;
      display: flex;
      flex-direction: column;
      gap: 0.25rem;
    }
    .stats-val {
      font-size: 1.35rem;
      font-weight: 700;
      color: var(--accent);
    }
    .stats-lbl {
      font-size: 0.78rem;
      color: var(--text-muted);
    }
    .dup-group {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 10px;
      margin-bottom: 0.9rem;
      overflow: hidden;
    }
    .dup-group-header {
      padding: 0.75rem 1rem;
      background: #1e293b;
      display: flex;
      justify-content: space-between;
      align-items: center;
      border-bottom: 1px solid var(--border);
      flex-wrap: wrap;
      gap: 0.5rem;
    }
    .dup-item {
      display: flex;
      align-items: center;
      gap: 0.8rem;
      padding: 0.75rem 1rem;
      border-bottom: 1px solid rgba(51, 65, 85, 0.5);
      flex-wrap: wrap;
    }
    .dup-item:last-child {
      border-bottom: none;
    }
    .badge-fmt {
      font-size: 0.72rem;
      font-weight: 700;
      padding: 3px 6px;
      border-radius: 4px;
      background: #334155;
      color: #f8fafc;
    }
    .badge-admin {
      background: rgba(234, 179, 8, 0.15);
      color: #eab308;
      border: 1px solid rgba(234, 179, 8, 0.35);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
    }
    .badge-user {
      background: rgba(148, 163, 184, 0.15);
      color: #94a3b8;
      border: 1px solid rgba(148, 163, 184, 0.35);
      font-size: 0.75rem;
      font-weight: 700;
      padding: 3px 8px;
      border-radius: 6px;
    }
    .collapsible-box {
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 1.2rem;
      margin-bottom: 1.2rem;
      display: none;
    }
    .data-table {
      width: 100%;
      border-collapse: collapse;
      margin-top: 0.8rem;
    }
    .data-table th, .data-table td {
      padding: 0.75rem 0.9rem;
      text-align: left;
      border-bottom: 1px solid var(--border);
      font-size: 0.88rem;
    }
    .data-table th {
      color: var(--text-muted);
      font-size: 0.75rem;
      text-transform: uppercase;
      letter-spacing: 0.5px;
    }
    .audio-player-bar {
      position: fixed;
      bottom: 0;
      left: 0;
      right: 0;
      background: rgba(15, 23, 42, 0.95);
      backdrop-filter: blur(12px);
      border-top: 1px solid rgba(29, 185, 84, 0.4);
      padding: 0.75rem 1.5rem;
      z-index: 9999;
      box-shadow: 0 -4px 25px rgba(0, 0, 0, 0.6);
      animation: slideUp 0.25s ease-out;
    }
    @keyframes slideUp {
      from { transform: translateY(100%); }
      to { transform: translateY(0); }
    }
    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0, 0, 0, 0.75);
      backdrop-filter: blur(4px);
      display: flex;
      align-items: center;
      justify-content: center;
      z-index: 10000;
      padding: 1rem;
    }
    .modal-box {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 1.5rem;
      width: 100%;
      max-width: 480px;
      box-shadow: 0 10px 30px rgba(0, 0, 0, 0.8);
    }
    .track-lib-item {
      display: flex;
      align-items: center;
      gap: 0.8rem;
      padding: 0.75rem 1rem;
      background: #0f172a;
      border: 1px solid var(--border);
      border-radius: 8px;
      transition: all 0.15s ease;
      flex-wrap: wrap;
    }
    .track-lib-item:hover {
      border-color: #3b82f6;
      background: #131d35;
    }
    .track-lib-item.active-playing {
      border-color: var(--primary);
      background: rgba(29, 185, 84, 0.08);
    }
    .badge-fmt-opus {
      background: rgba(16, 185, 129, 0.15);
      color: #10b981;
      border: 1px solid rgba(16, 185, 129, 0.3);
    }
    .badge-fmt-mp3 {
      background: rgba(59, 130, 246, 0.15);
      color: #60a5fa;
      border: 1px solid rgba(59, 130, 246, 0.3);
    }
    .badge-fmt-flac {
      background: rgba(168, 85, 247, 0.15);
      color: #c084fc;
      border: 1px solid rgba(168, 85, 247, 0.3);
    }
    .badge-fmt-m4a {
      background: rgba(245, 158, 11, 0.15);
      color: #fbbf24;
      border: 1px solid rgba(245, 158, 11, 0.3);
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🎵 Spoti-Downloader & Navidrome</h1>
      <p>Prywatny ekosystem muzyczny • On-Demand Spotify / YouTube to Opus Engine</p>
    </header>

    <!-- GŁÓWNA NAWIGACJA ZAKŁADKOWA -->
    <div class="tabs-nav">
      <button class="tab-btn active" id="tabBtn-download" onclick="switchTab('download')">📥 Pobieranie i Wyszukiwanie</button>
      <button class="tab-btn" id="tabBtn-library" onclick="switchTab('library')">📚 Biblioteka Utworów</button>
      <button class="tab-btn" id="tabBtn-duplicates" onclick="switchTab('duplicates')">🧹 Wykrywanie Duplikatów</button>
      <button class="tab-btn" id="tabBtn-playlists" onclick="switchTab('playlists')">📑 Playlisty</button>
      <button class="tab-btn" id="tabBtn-users" onclick="switchTab('users')">👥 Użytkownicy</button>
    </div>

    <!-- ZAKŁADKA 1: POBIERANIE I WYSZUKIWANIE -->
    <div id="tab-download" class="tab-content active">
    <!-- KARTA GŁÓWNA: POBIERANIE I WYSZUKIWANIE -->
    <div class="card">
      <div class="form-group">
        <label for="queryInput">Wklej link ze Spotify / YouTube (playlista, album, utwór) lub wpisz nazwę:</label>
        <div style="display: flex; gap: 0.6rem;">
          <input type="text" id="queryInput" placeholder="np. link do playlisty Spotify / YouTube lub Dawid Podsiadło" onkeydown="if(event.key==='Enter') handlePrimaryAction()">
          <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0 1.4rem;" onclick="handlePrimaryAction()">🔍 Szukaj</button>
        </div>
      </div>
      
      <div style="display: flex; gap: 1rem; margin-bottom: 0.8rem; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 160px;">
          <label for="searchTypeSelect">Typ wyszukiwania tekstowego:</label>
          <select id="searchTypeSelect" onchange="if(document.getElementById('queryInput').value.trim()) searchMusic()">
            <option value="track" selected>🎵 Utwory</option>
            <option value="album">💿 Albumy</option>
            <option value="playlist">📑 Playlisty</option>
          </select>
        </div>
        <div style="flex: 1; min-width: 160px;">
          <label for="formatSelect">Format wyjściowy:</label>
          <select id="formatSelect">
            <option value="opus" selected>Opus ~160 kbps (Zoptymalizowany, wysoka jakość)</option>
            <option value="mp3">MP3 320 kbps (Maksymalna kompatybilność)</option>
            <option value="flac">FLAC (Bezstratny)</option>
          </select>
        </div>
      </div>

      <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1.2rem;">
        <input type="checkbox" id="forceCheck" style="width: auto; cursor: pointer;">
        <label for="forceCheck" style="margin-bottom: 0; cursor: pointer; color: var(--text-muted); font-size: 0.88rem;">
          Wymuś ponowne pobranie i nadpisanie plików (odświeża okładki albumów i zastępuje skity czystym audio)
        </label>
      </div>

      <div class="btn-row" style="flex-wrap: wrap;">
        <button class="btn-primary" onclick="inspectPlaylistOrLink()">📋 Wybierz utwory z playlisty / linku (Ręczny)</button>
        <button class="btn-secondary" onclick="startDownload(null, true)">⚡ Szybkie pobranie w tle (Automat)</button>
      </div>

      <div id="statusBox" class="status-box"></div>
      <div id="searchResults" class="results-grid"></div>
    </div>

    <!-- KARTA INSPEKTORA: RĘCZNY WYBÓR I EDYCJA METADANYCH UTWORÓW -->
    <div id="inspectorCard" class="inspector-card" style="display: none;">
      <div style="display: flex; align-items: center; justify-content: space-between; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 1rem;">
        <div style="display: flex; align-items: center; gap: 1rem;">
          <img id="inspectorCover" src="" style="width: 72px; height: 72px; border-radius: 8px; object-fit: cover;">
          <div>
            <div style="display: flex; align-items: center; gap: 0.5rem;">
              <span id="inspectorBadge" class="badge">SPOTIFY</span>
              <h2 id="inspectorTitle" style="font-size: 1.25rem; font-weight: 700;"></h2>
            </div>
            <div id="inspectorSubtitle" style="color: var(--text-muted); font-size: 0.88rem; margin-top: 0.25rem;"></div>
          </div>
        </div>
        <div>
          <button class="btn-primary" style="padding: 0.75rem 1.4rem; font-size: 0.95rem;" onclick="downloadSelectedTracks()">
            ⬇️ Pobierz zaznaczone z oficjalnymi albumami (<span id="selectedCount">0</span>)
          </button>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem;">
        <div style="display: flex; gap: 0.6rem; flex-wrap: wrap;">
          <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAll(true)">☑️ Zaznacz wszystkie</button>
          <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAll(false)">⬜ Odznacz wszystkie</button>
          <button class="btn-secondary btn-danger-soft" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="removeDuplicateTracksFromList()" title="Usuwa z widoku kafelki z utworami, które znajdują się już w bazie">🗑️ Usuń duplikaty z bazy</button>
        </div>
        <div style="color: var(--text-muted); font-size: 0.82rem;">
          💡 Lista utworów. Kliknij „🔍 Wybierz wersję”, aby zobaczyć oficjalne albumy i okładki, lub „⬇️ Pobierz”.
        </div>
      </div>

      <div id="inspectorTracksList" style="display: flex; flex-direction: column; gap: 0.7rem;"></div>
    </div>

    <!-- KARTA ZARZĄDZANIA BIBLIOTEKĄ -->
    <div class="card" style="border-color: #334155;">
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
        <div>
          <div style="font-weight: 600; font-size: 0.95rem;">Zarządzanie biblioteką muzyczną</div>
          <div style="color: var(--text-muted); font-size: 0.82rem;">Odśwież bazę Navidrome lub wyczyść całą zawartość i zacznij od nowa</div>
        </div>
        <div style="display: flex; gap: 0.8rem; flex-wrap: wrap;">
          <button class="btn-secondary" style="padding: 0.6rem 1.1rem; font-size: 0.88rem;" onclick="refreshNavidrome()">🔄 Odśwież Navidrome</button>
          <button class="btn-secondary" style="padding: 0.6rem 1.1rem; font-size: 0.88rem; background: #7f1d1d; border-color: #991b1b; color: #fecaca;" onclick="clearLibrary()">🗑️ Wyczyść całą bibliotekę</button>
        </div>
      </div>
    </div>
    </div>
    <!-- KONIEC ZAKŁADKI 1: POBIERANIE -->

    <!-- ZAKŁADKA 2: BIBLIOTEKA UTWORÓW -->
    <div id="tab-library" class="tab-content">
      <!-- KARTA STATYSTYK BIBLIOTEKI -->
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.2rem;">
          <div>
            <h2 style="font-size: 1.25rem; font-weight: 700; color: #fff;">📚 Twoja Biblioteka Muzyczna</h2>
            <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem;">
              Przeglądaj wszystkie pobrane utwory, odsłuchuj audio w przeglądarce, zarządzaj plikami i twórz playlisty.
            </p>
          </div>
          <div style="display: flex; gap: 0.6rem; flex-wrap: wrap;">
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.65rem 1.2rem;" onclick="loadLibraryTracks(true)">
              🔄 Odśwież listę
            </button>
          </div>
        </div>

        <div id="libraryStatusBox" class="status-box" style="display: none;"></div>

        <!-- Pasek statystyk biblioteki -->
        <div class="stats-card">
          <div class="stats-item">
            <span class="stats-val" id="statLibTracks">0</span>
            <span class="stats-lbl">Utworów w bibliotece</span>
          </div>
          <div class="stats-item">
            <span class="stats-val" id="statLibSize">0 MB</span>
            <span class="stats-lbl">Rozmiar na dysku</span>
          </div>
          <div class="stats-item">
            <span class="stats-val" id="statLibArtists">0</span>
            <span class="stats-lbl">Wykonawców</span>
          </div>
          <div class="stats-item">
            <span class="stats-val" id="statLibAlbums">0</span>
            <span class="stats-lbl">Albumów</span>
          </div>
        </div>
      </div>

      <!-- KARTA PRZEGLĄDARKI I FILTRÓW -->
      <div class="card">
        <div style="display: flex; gap: 0.8rem; margin-bottom: 1rem; flex-wrap: wrap; align-items: center;">
          <div style="flex: 2; min-width: 200px;">
            <input type="text" id="librarySearchInput" placeholder="🔍 Szukaj po tytule, wykonawcy, albumie..." oninput="handleLibraryFilterChange()">
          </div>
          <div style="flex: 1; min-width: 140px;">
            <select id="libraryFormatSelect" onchange="handleLibraryFilterChange()">
              <option value="ALL">Wszystkie formaty</option>
              <option value="OPUS">Tylko OPUS</option>
              <option value="MP3">Tylko MP3</option>
              <option value="FLAC">Tylko FLAC</option>
              <option value="M4A">Tylko M4A</option>
            </select>
          </div>
          <div style="flex: 1; min-width: 170px;">
            <select id="librarySortSelect" onchange="handleLibraryFilterChange()">
              <option value="newest" selected>📅 Najnowsze pobrane</option>
              <option value="oldest">📅 Najstarsze pobrane</option>
              <option value="title_asc">🔤 Tytuł (A-Z)</option>
              <option value="title_desc">🔤 Tytuł (Z-A)</option>
              <option value="artist_asc">🎤 Wykonawca (A-Z)</option>
              <option value="size_desc">💾 Rozmiar (największe)</option>
              <option value="duration_desc">⏱️ Czas trwania (najdłuższe)</option>
            </select>
          </div>
        </div>

        <!-- Pasek akcji masowych -->
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem; padding: 0.6rem 0.8rem; background: #0f172a; border-radius: 8px; border: 1px solid var(--border);">
          <div style="display: flex; gap: 0.6rem; align-items: center;">
            <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAllLibrary(true)">☑️ Zaznacz wszystkie</button>
            <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAllLibrary(false)">⬜ Odznacz</button>
            <span style="color: var(--text-muted); font-size: 0.82rem; margin-left: 0.5rem;" id="libraryFilterCountInfo">Widoczne: 0 / 0</span>
          </div>
          <div>
            <button id="btnDeleteSelectedLib" class="btn-secondary btn-danger-soft" style="padding: 0.45rem 0.95rem; font-size: 0.82rem; display: none;" onclick="deleteSelectedLibraryTracks()">
              🗑️ Usuń zaznaczone (<span id="selectedLibCount">0</span>)
            </button>
          </div>
        </div>

        <!-- Lista utworów -->
        <div id="libraryTracksList" style="display: flex; flex-direction: column; gap: 0.5rem;">
          <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
            ⏳ Wczytywanie biblioteki utworów...
          </div>
        </div>
      </div>

      <!-- KARTA ADMINISTRACJI I ZARZĄDZANIA BIBLIOTEKĄ -->
      <div class="card" style="border-color: #334155;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem;">
          <div>
            <div style="font-weight: 600; font-size: 0.95rem;">Konserwacja biblioteki muzycznej</div>
            <div style="color: var(--text-muted); font-size: 0.82rem;">Wymuś reskan w Navidrome lub całkowicie wyczyść bazę danych</div>
          </div>
          <div style="display: flex; gap: 0.8rem; flex-wrap: wrap;">
            <button class="btn-secondary" style="padding: 0.6rem 1.1rem; font-size: 0.88rem;" onclick="refreshNavidrome()">🔄 Odśwież Navidrome</button>
            <button class="btn-secondary" style="padding: 0.6rem 1.1rem; font-size: 0.88rem; background: #7f1d1d; border-color: #991b1b; color: #fecaca;" onclick="clearLibrary()">🗑️ Wyczyść całą bibliotekę</button>
          </div>
        </div>
      </div>
    </div>
    <!-- KONIEC ZAKŁADKI 2: BIBLIOTEKA -->

    <!-- ZAKŁADKA 3: WYKRYWANIE DUPLIKATÓW -->
    <div id="tab-duplicates" class="tab-content">
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.2rem;">
          <div>
            <h2 style="font-size: 1.25rem; font-weight: 700; color: #fff;">🧹 Wykrywanie i Usuwanie Duplikatów</h2>
            <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem;">
              Automatycznie przeszukuje pliki na dysku <code>/music</code> i grupuje utwory o tym samym tytule.
            </p>
          </div>
          <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.65rem 1.3rem;" onclick="scanDuplicates()">
            🔍 Skanuj bibliotekę
          </button>
        </div>

        <div id="duplicatesStatusBox" class="status-box" style="display: none;"></div>

        <!-- Pasek statystyk i akcji masowych -->
        <div id="duplicatesStatsBanner" style="display: none;">
          <div class="stats-card">
            <div class="stats-item">
              <span class="stats-val" id="statDupGroups">0</span>
              <span class="stats-lbl">Grup duplikatów</span>
            </div>
            <div class="stats-item">
              <span class="stats-val" id="statDupFiles">0</span>
              <span class="stats-lbl">Zbędnych plików</span>
            </div>
            <div class="stats-item">
              <span class="stats-val" id="statWastedSpace">0 MB</span>
              <span class="stats-lbl">Możliwe do odzyskania</span>
            </div>
          </div>

          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem;">
            <div style="display: flex; gap: 0.6rem; flex-wrap: wrap;">
              <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="selectNonFirstDuplicates()">
                ☑️ Zaznacz zbędne (zostaw 1 najlepszy)
              </button>
              <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleAllDuplicates(false)">
                ⬜ Odznacz wszystkie
              </button>
            </div>
            <button class="btn-secondary btn-danger-soft" style="padding: 0.55rem 1.1rem; font-size: 0.85rem; font-weight: 700;" onclick="deleteSelectedDuplicates()">
              🗑️ Usuń zaznaczone duplikaty (<span id="selectedDupFilesCount">0</span>)
            </button>
          </div>
        </div>

        <div id="duplicatesList"></div>
      </div>
    </div>

    <!-- ZAKŁADKA 3: PLAYLISTY -->
    <div id="tab-playlists" class="tab-content">
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.2rem;">
          <div>
            <h2 style="font-size: 1.25rem; font-weight: 700; color: #fff;">📑 Playlisty w Navidrome</h2>
            <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem;">
              Zarządzaj swoimi playlistami zsynchronizowanymi z Subsonic (Feishin, Symfonium, Amperfy).
            </p>
          </div>
          <div style="display: flex; gap: 0.6rem;">
            <button class="btn-secondary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1rem; font-size: 0.88rem;" onclick="loadPlaylists()">🔄 Odśwież</button>
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1.2rem; font-size: 0.88rem;" onclick="toggleCreatePlaylistBox()">➕ Nowa playlista</button>
          </div>
        </div>

        <div id="playlistsStatusBox" class="status-box" style="display: none;"></div>

        <!-- Formularz tworzenia playlisty -->
        <div id="createPlaylistBox" class="collapsible-box">
          <h3 style="font-size: 1rem; margin-bottom: 0.8rem; color: #fff;">Utwórz nową playlistę</h3>
          <div style="display: flex; gap: 0.6rem; flex-wrap: wrap;">
            <input type="text" id="newPlaylistName" placeholder="Nazwa playlisty (np. Samochód, Ulubione 2026...)" style="flex: 1; min-width: 220px;" onkeydown="if(event.key==='Enter') submitCreatePlaylist()">
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0 1.4rem;" onclick="submitCreatePlaylist()">Zapisz playlistę</button>
            <button class="btn-secondary" style="flex: 0 0 auto; width: auto; padding: 0 1rem;" onclick="toggleCreatePlaylistBox(false)">Anuluj</button>
          </div>
        </div>

        <!-- Widok szczegółów wybranej playlisty -->
        <div id="activePlaylistDetailBox" class="collapsible-box" style="border-color: var(--accent);">
          <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem;">
            <div>
              <span class="badge" style="margin-bottom: 0.3rem;">OTWARTA PLAYLISTA</span>
              <h3 id="activePlaylistNameHeader" style="font-size: 1.2rem; color: #fff;"></h3>
              <div id="activePlaylistSubheader" style="color: var(--text-muted); font-size: 0.82rem;"></div>
            </div>
            <button class="btn-secondary" style="flex: 0 0 auto; width: auto; padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="closePlaylistDetail()">⬅️ Zamknij podgląd</button>
          </div>

          <!-- Wyszukiwarka utworów do dodania -->
          <div style="background: #1e293b; padding: 0.9rem; border-radius: 8px; margin-bottom: 1rem; border: 1px solid var(--border);">
            <label style="font-size: 0.82rem; margin-bottom: 0.4rem;">Dodaj utwór z biblioteki do tej playlisty:</label>
            <div style="display: flex; gap: 0.6rem;">
              <input type="text" id="searchSongToAddInput" placeholder="Wpisz tytuł lub wykonawcę z bazy Navidrome..." onkeydown="if(event.key==='Enter') searchSongsForPlaylist()">
              <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0 1.2rem; font-size: 0.85rem;" onclick="searchSongsForPlaylist()">🔍 Szukaj</button>
            </div>
            <div id="searchSongsResults" style="margin-top: 0.6rem; display: flex; flex-direction: column; gap: 0.4rem;"></div>
          </div>

          <!-- Lista utworów w playliście -->
          <div id="activePlaylistTracksList" style="display: flex; flex-direction: column; gap: 0.4rem;"></div>
        </div>

        <!-- Lista wszystkich playlist -->
        <div id="playlistsList" style="display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 0.8rem;"></div>
      </div>
    </div>

    <!-- ZAKŁADKA 4: UŻYTKOWNICY -->
    <div id="tab-users" class="tab-content">
      <div class="card">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 1rem; margin-bottom: 1.2rem;">
          <div>
            <h2 style="font-size: 1.25rem; font-weight: 700; color: #fff;">👥 Użytkownicy Navidrome</h2>
            <p style="color: var(--text-muted); font-size: 0.85rem; margin-top: 0.2rem;">
              Zarządzaj kontami użytkowników, hasłami oraz uprawnieniami administratora.
            </p>
          </div>
          <div style="display: flex; gap: 0.6rem;">
            <button class="btn-secondary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1rem; font-size: 0.88rem;" onclick="loadUsers()">🔄 Odśwież</button>
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1.2rem; font-size: 0.88rem;" onclick="toggleCreateUserBox()">➕ Nowy użytkownik</button>
          </div>
        </div>

        <div id="usersStatusBox" class="status-box" style="display: none;"></div>

        <!-- Formularz tworzenia użytkownika -->
        <div id="createUserBox" class="collapsible-box">
          <h3 style="font-size: 1rem; margin-bottom: 0.8rem; color: #fff;">Stwórz nowe konto użytkownika</h3>
          <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 0.8rem; margin-bottom: 0.8rem;">
            <div>
              <label>Login (nazwa konta):</label>
              <input type="text" id="newUsername" placeholder="np. jowita">
            </div>
            <div>
              <label>Imię / Nazwa wyświetlana:</label>
              <input type="text" id="newName" placeholder="np. Jowita">
            </div>
            <div>
              <label>Hasło:</label>
              <input type="password" id="newPassword" placeholder="Hasło użytkownika">
            </div>
            <div>
              <label>E-mail (opcjonalny):</label>
              <input type="email" id="newEmail" placeholder="np. jowita@example.com">
            </div>
          </div>
          <div style="display: flex; align-items: center; gap: 0.6rem; margin-bottom: 1rem;">
            <input type="checkbox" id="newIsAdmin" style="width: auto; cursor: pointer;">
            <label for="newIsAdmin" style="margin-bottom: 0; cursor: pointer; color: #fff; font-size: 0.88rem;">
              👑 Nadaj uprawnienia Administratora
            </label>
          </div>
          <div style="display: flex; gap: 0.6rem;">
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1.5rem;" onclick="submitCreateUser()">Utwórz konto</button>
            <button class="btn-secondary" style="flex: 0 0 auto; width: auto; padding: 0.6rem 1rem;" onclick="toggleCreateUserBox(false)">Anuluj</button>
          </div>
        </div>

        <!-- Lista kont -->
        <div id="usersListContainer"></div>
      </div>
    </div>

    <div class="nav-links">
      <a href="http://" + window.location.hostname + ":4533" target="_blank" id="navidromeLink">Otwórz Navidrome Web UI ↗</a>
      <a href="/docs" target="_blank">Dokumentacja Swagger API ↗</a>
    </div>
  </div>

  <script>
    // Globalny łapacz błędów JavaScript
    window.onerror = function(msg, url, lineNo, columnNo, error) {
      const sb = document.getElementById("statusBox");
      if (sb) {
        sb.style.display = "block";
        sb.innerHTML = `<span style="color: #ef4444;">❌ Błąd interfejsu (linia ${lineNo}): ${msg}</span>`;
      }
      return false;
    };

    const isProxied = window.location.pathname.startsWith('/dl');
    const API_BASE = isProxied ? '/dl' : '';
    const navidromeUrl = isProxied ? (window.location.origin + '/') : ('http://' + window.location.hostname + ':4533');
    document.getElementById("navidromeLink").href = navidromeUrl;

    let currentInspectedTracks = [];
    let currentPlaylistName = "";

    function escapeHtml(text) {
      if (!text) return '';
      return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
    }

    function formatDuration(ms) {
      if (!ms || ms <= 0) return '';
      const totalSeconds = Math.round(ms / 1000);
      const minutes = Math.floor(totalSeconds / 60);
      const seconds = totalSeconds % 60;
      return `${minutes}:${seconds < 10 ? '0' : ''}${seconds}`;
    }

    function handlePrimaryAction() {
      const q = document.getElementById('queryInput').value.trim();
      if (!q) return;
      if (q.includes('spotify.com/') || q.includes('youtube.com/') || q.includes('youtu.be/')) {
        inspectPlaylistOrLink();
      } else {
        searchMusic();
      }
    }

    // --- INSPEKTOR PLAYLISTY / LINKU ---
    async function inspectPlaylistOrLink() {
      const q = document.getElementById('queryInput').value.trim();
      const statusBox = document.getElementById('statusBox');
      const inspCard = document.getElementById('inspectorCard');
      const resultsContainer = document.getElementById('searchResults');

      if (!q) {
        alert("Wklej link ze Spotify (playlista/album/utwór) lub YouTube (playlista/wideo)!");
        return;
      }

      statusBox.style.display = "block";
      statusBox.innerHTML = "⏳ Analizowanie linku i pobieranie metadanych utworów...";
      resultsContainer.innerHTML = "";
      inspCard.style.display = "none";

      try {
        const res = await fetch(`${API_BASE}/inspect`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ url_or_query: q })
        });
        const data = await res.json();

        if (!res.ok) {
          statusBox.innerHTML = `❌ Błąd inspekcji: ${data.detail || 'Nie udało się odczytać linku'}`;
          return;
        }

        statusBox.style.display = "none";
        currentInspectedTracks = data.tracks || [];
        currentPlaylistName = data.title || "";

        document.getElementById('inspectorBadge').innerText = (data.source || 'LINK').toUpperCase();
        document.getElementById('inspectorTitle').innerText = data.title || "Znalezione utwory";
        document.getElementById('inspectorSubtitle').innerText = `${data.owner || ''} • ${currentInspectedTracks.length} utworów`;
        
        const coverEl = document.getElementById('inspectorCover');
        if (data.cover_url) {
          coverEl.src = data.cover_url;
          coverEl.style.display = "block";
        } else {
          coverEl.style.display = "none";
        }

        renderInspectorTracks();
        inspCard.style.display = "block";
        inspCard.scrollIntoView({ behavior: 'smooth' });

      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    function renderInspectorTracks() {
      const container = document.getElementById('inspectorTracksList');
      container.innerHTML = "";

      currentInspectedTracks.forEach((t, idx) => {
        const div = document.createElement('div');
        div.className = 'track-row';
        div.id = `track-row-${idx}`;
        div.style.cssText = "display: flex; flex-direction: column; align-items: stretch; gap: 0.5rem; padding: 0.8rem; background: #1e293b; border-radius: 8px; border: 1px solid var(--border);";

        const durStr = formatDuration(t.duration_ms);
        const inLib = Boolean(t.in_library);
        const isChecked = !inLib;

        div.innerHTML = `
          <div style="display: flex; align-items: center; gap: 0.7rem; width: 100%; flex-wrap: wrap;">
            <input type="checkbox" id="check-${idx}" style="width: 20px; height: 20px; cursor: pointer;" ${isChecked ? 'checked' : ''} onchange="updateSelectedCounter()">
            <span style="color: var(--text-muted); font-size: 0.85rem; font-weight: 700; width: 30px; text-align: right; flex-shrink: 0;">#${idx + 1}</span>
            <div style="flex: 1; display: grid; grid-template-columns: 1fr 1fr; gap: 0.6rem; min-width: 240px;">
              <div>
                <span class="field-lbl">Tytuł:</span>
                <input type="text" id="title-${idx}" class="input-compact" value="${escapeHtml(t.title)}">
              </div>
              <div>
                <span class="field-lbl">Wykonawca:</span>
                <input type="text" id="artist-${idx}" class="input-compact" value="${escapeHtml(t.artist)}">
              </div>
            </div>
            ${durStr ? `<span class="badge-duration" title="Długość utworu">⏱️ ${durStr}</span>` : ''}
            ${inLib ? `<span class="badge-in-library" title="Ten utwór został już znaleziony w lokalnej bibliotece">⚠️ Ten utwór już jest w bazie</span>` : ''}
            <div style="display: flex; gap: 0.5rem; align-items: center; flex-shrink: 0; margin-left: auto;">
              <button class="btn-secondary" onclick="playOnlinePreview(document.getElementById('artist-${idx}').value + ' ' + document.getElementById('title-${idx}').value, document.getElementById('title-${idx}').value, document.getElementById('artist-${idx}').value)" style="padding: 0.45rem 0.75rem; font-size: 0.82rem; white-space: nowrap;" title="Odsłuchaj fragment audio przed pobraniem">
                ▶️ Odsłuchaj
              </button>
              <button class="btn-search-version" id="search-btn-${idx}" onclick="lookupTrackVersions(${idx})" title="Wyszukaj ten utwór w katalogu, zobacz oficjalne albumy i wybierz wersję">
                🔍 Wybierz wersję
              </button>
              <button class="download-small-btn" id="dl-btn-${idx}" onclick="downloadSingleTrackFromList(${idx})" title="Pobierz ten utwór z oficjalnymi metadanymi" style="width: max-content; flex: 0 0 auto;">
                ${inLib ? '⬇️ Pobierz ponownie' : '⬇️ Pobierz'}
              </button>
            </div>
          </div>
          <div id="versions-${idx}" class="versions-panel" style="display: none;"></div>
        `;
        container.appendChild(div);
      });

      updateSelectedCounter();
    }

    function updateSelectedCounter() {
      let count = 0;
      currentInspectedTracks.forEach((_, idx) => {
        const row = document.getElementById(`track-row-${idx}`);
        if (row && row.style.display === 'none') return;
        const cb = document.getElementById(`check-${idx}`);
        if (cb && cb.checked) count++;
      });
      document.getElementById('selectedCount').innerText = count;
    }

    function toggleSelectAll(checked) {
      currentInspectedTracks.forEach((_, idx) => {
        const row = document.getElementById(`track-row-${idx}`);
        if (row && row.style.display === 'none') return;
        const cb = document.getElementById(`check-${idx}`);
        if (cb) cb.checked = checked;
      });
      updateSelectedCounter();
    }

    function removeDuplicateTracksFromList() {
      let removedCount = 0;
      currentInspectedTracks.forEach((t, idx) => {
        if (t.in_library) {
          const row = document.getElementById(`track-row-${idx}`);
          if (row && row.style.display !== 'none') {
            removeTrackRowWithAnimation(idx);
            removedCount++;
          }
        }
      });
      if (removedCount > 0) {
        showToast(`🗑️ Usunięto z widoku ${removedCount} utworów obecnych w bazie.`);
      } else {
        showToast(`ℹ️ Brak utworów do usunięcia (żaden utwór na liście nie jest w bazie).`);
      }
    }

    async function lookupTrackVersions(idx) {
      const panel = document.getElementById(`versions-${idx}`);
      if (panel.style.display === 'block') {
        panel.style.display = 'none';
        return;
      }

      const title = document.getElementById(`title-${idx}`).value.trim();
      const artist = document.getElementById(`artist-${idx}`).value.trim();
      const query = `${artist} ${title}`.trim() || title;

      panel.style.display = 'block';
      panel.innerHTML = `<div style="padding: 0.6rem; color: var(--text-muted); font-size: 0.85rem;">⏳ Wyszukiwanie oficjalnych wydań na Spotify / w katalogu...</div>`;

      try {
        const res = await fetch(`${API_BASE}/search?q=${encodeURIComponent(query)}&type=track&limit=5`);
        const data = await res.json();
        if (!res.ok || !data.results || data.results.length === 0) {
          panel.innerHTML = `<div style="padding: 0.6rem; color: #f59e0b; font-size: 0.85rem;">⚠️ Nie znaleziono bezpośrednich wydań w katalogu. Możesz pobrać utwór przyciskiem „⬇️ Pobierz” (downloader automatycznie dobierze studyjne audio).</div>`;
          return;
        }

        panel.innerHTML = `
          <div style="font-size: 0.78rem; font-weight: 700; color: var(--accent); margin-bottom: 0.5rem; text-transform: uppercase;">
            💿 Oficjalne albumy i wydania (wybierz wersję):
          </div>
          <div>
            ${data.results.map(item => {
              const q = item.spotify_url || (item.artist + ' - ' + item.title);
              const encQ = encodeURIComponent(q);
              const durStr = formatDuration(item.duration_ms);
              const inLib = Boolean(item.in_library);
              return `
              <div class="version-item">
                <div style="display: flex; align-items: center; gap: 0.7rem; min-width: 0; flex: 1;">
                  <img src="${item.cover_url || 'https://via.placeholder.com/44'}" alt="cover">
                  <div style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;">
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                      <span style="font-weight: 600; font-size: 0.88rem; color: #fff;">${escapeHtml(item.title)}</span>
                      ${durStr ? `<span class="badge-duration" style="font-size: 0.75rem; padding: 2px 6px;">⏱️ ${durStr}</span>` : ''}
                      ${inLib ? `<span class="badge-in-library" style="font-size: 0.75rem; padding: 2px 6px;">⚠️ Ten utwór już jest w bazie</span>` : ''}
                    </div>
                    <div style="font-size: 0.78rem; color: var(--text-muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis;">
                      ${escapeHtml(item.artist)} • <b style="color: #cbd5e1;">${escapeHtml(item.album)}</b> (${escapeHtml(item.year || '')})
                    </div>
                  </div>
                </div>
                <div style="display: flex; gap: 0.4rem; align-items: center; flex: 0 0 auto; margin-left: auto;">
                  <button class="btn-secondary" onclick="playOnlinePreview(decodeURIComponent('${encQ}'), decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.artist)}'))" style="padding: 0.45rem 0.75rem; font-size: 0.82rem; white-space: nowrap;" title="Odsłuchaj fragment audio przed pobraniem">
                    ▶️ Odsłuchaj
                  </button>
                  <button class="download-small-btn" onclick="downloadVersionFromList(${idx}, decodeURIComponent('${encQ}'))" style="flex: 0 0 auto; width: max-content; padding: 0.45rem 0.85rem; font-size: 0.82rem; white-space: nowrap;">
                    ⬇️ Pobierz tę wersję
                  </button>
                </div>
              </div>
            `;
            }).join('')}
          </div>
        `;
      } catch (err) {
        panel.innerHTML = `<div style="padding: 0.6rem; color: #ef4444; font-size: 0.85rem;">Błąd wyszukiwania: ${err}</div>`;
      }
    }

    function showToast(message) {
      let toast = document.getElementById('floatingToast');
      if (!toast) {
        toast = document.createElement('div');
        toast.id = 'floatingToast';
        toast.className = 'toast';
        document.body.appendChild(toast);
      }
      toast.innerHTML = message;
      toast.style.display = 'flex';
      clearTimeout(window._toastTimeout);
      window._toastTimeout = setTimeout(() => {
        toast.style.display = 'none';
      }, 4000);
    }

    function removeTrackRowWithAnimation(idx) {
      const row = document.getElementById(`track-row-${idx}`);
      if (row) {
        row.style.transition = "all 0.35s ease";
        row.style.opacity = "0";
        row.style.transform = "translateX(50px)";
        setTimeout(() => {
          row.style.display = "none";
          const cb = document.getElementById(`check-${idx}`);
          if (cb) cb.checked = false;
          updateSelectedCounter();
        }, 350);
      }
    }

    function downloadVersionFromList(idx, query) {
      removeTrackRowWithAnimation(idx);
      startDownload(query, false);
    }

    function downloadSingleTrackFromList(idx) {
      const title = document.getElementById(`title-${idx}`).value.trim();
      const artist = document.getElementById(`artist-${idx}`).value.trim();
      const original = currentInspectedTracks[idx];
      const query = (artist && title) ? `${artist} - ${title}` : (original.spotify_url || title);
      removeTrackRowWithAnimation(idx);
      startDownload(query, false);
    }

    async function downloadSelectedTracks() {
      const selectedTracks = [];
      const selectedIndices = [];
      currentInspectedTracks.forEach((_, idx) => {
        const row = document.getElementById(`track-row-${idx}`);
        if (row && row.style.display === 'none') return;
        const cb = document.getElementById(`check-${idx}`);
        if (cb && cb.checked) {
          selectedIndices.push(idx);
          const title = document.getElementById(`title-${idx}`).value.trim();
          const artist = document.getElementById(`artist-${idx}`).value.trim();
          const original = currentInspectedTracks[idx];
          selectedTracks.push({
            spotify_id: original.spotify_id || `item_${idx}`,
            title: title || original.title,
            artists: [artist || original.artist],
            artist: artist || original.artist,
            album: "", // Pozostaw puste, aby backend dopasował oficjalny album ze Spotify
            album_artist: artist || original.artist,
            track_number: idx + 1,
            total_tracks: currentInspectedTracks.length,
            disc_number: 1,
            release_date: "",
            year: "",
            duration_ms: original.duration_ms || 0,
            cover_url: null, // Pozostaw null, aby backend pobrał oficjalną okładkę albumu
            isrc: original.isrc || null,
            spotify_url: original.spotify_url || "",
            youtube_url: original.youtube_url || null
          });
        }
      });

      if (selectedTracks.length === 0) {
        alert("Zaznacz co najmniej jeden utwór do pobrania!");
        return;
      }

      const format = document.getElementById('formatSelect').value;
      const force = document.getElementById('forceCheck').checked;
      const statusBox = document.getElementById('statusBox');

      statusBox.style.display = "block";
      statusBox.innerHTML = `⏳ Kolejkowanie ${selectedTracks.length} wybranych utworów z oficjalnymi metadanymi...`;
      showToast(`⏳ Kolejkowanie ${selectedTracks.length} wybranych utworów...`);

      // Ukryj pobierane kafelki z animacją
      selectedIndices.forEach(idx => removeTrackRowWithAnimation(idx));

      try {
        const res = await fetch(`${API_BASE}/download-selected`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tracks: selectedTracks,
            format: format,
            force: force,
            playlist_name: currentPlaylistName
          })
        });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> Pobieranie ${selectedTracks.length} utworów z oficjalnymi albumami i okładkami...`;
          showToast(`🚀 <b>Pobieranie ${selectedTracks.length} utworów w toku!</b>`);
          pollTask(data.task_id);
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
          showToast(`❌ Błąd: ${data.detail || data.message}`);
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
        showToast(`❌ Błąd połączenia: ${err}`);
      }
    }

    // --- AUTOMATYCZNE POBRANIE W TLE ---
    async function startDownload(customQuery = null, shouldScroll = false) {
      const q = customQuery || document.getElementById('queryInput').value.trim();
      const format = document.getElementById('formatSelect').value;
      const force = document.getElementById('forceCheck').checked;
      const statusBox = document.getElementById('statusBox');

      if (!q) {
        alert("Wpisz zapytanie lub wklej link Spotify / YouTube!");
        return;
      }

      statusBox.style.display = "block";
      statusBox.innerHTML = "⏳ Kolejkowanie zadania w tle...";
      if (shouldScroll) {
        statusBox.scrollIntoView({ behavior: 'smooth' });
      }
      showToast(`⏳ Kolejkowanie: <b>${escapeHtml(q)}</b>...`);

      try {
        const res = await fetch(`${API_BASE}/download`, {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ query_or_url: q, format: format, force: force })
        });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> Trwa pobieranie...`;
          showToast(`🚀 <b>Zadanie zlecone:</b> ${escapeHtml(q)}`);
          pollTask(data.task_id);
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
          showToast(`❌ Błąd: ${data.detail || data.message}`);
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
        showToast(`❌ Błąd połączenia: ${err}`);
      }
    }

    async function pollTask(taskId) {
      const statusBox = document.getElementById('statusBox');
      const interval = setInterval(async () => {
        try {
          const res = await fetch(`${API_BASE}/tasks/${taskId}`);
          const task = await res.json();
          if (task.status === 'processing') {
            statusBox.innerHTML = `⚙️ <b>Pobieranie w toku:</b> ${task.current_track || ''} (${task.completed_tracks}/${task.total_tracks})`;
          } else if (task.status === 'success') {
            clearInterval(interval);
            statusBox.innerHTML = `✅ <b>Gotowe!</b> Pobrano ${task.completed_tracks} utworów. Biblioteka Navidrome została natychmiast zaktualizowana!`;
            showToast(`✅ <b>Gotowe!</b> Pobrano ${task.completed_tracks} utworów.`);
          } else if (task.status === 'failed') {
            clearInterval(interval);
            statusBox.innerHTML = `❌ <b>Błąd zadania:</b> ${task.error_message || 'Nieznany błąd'}`;
            showToast(`❌ Błąd pobierania: ${task.error_message || ''}`);
          }
        } catch (e) {
          clearInterval(interval);
        }
      }, 1500);
    }

    // --- WYSZUKIWANIE SPOTIFY ---
    async function searchMusic() {
      const q = document.getElementById('queryInput').value.trim();
      const type = document.getElementById('searchTypeSelect').value;
      const resultsContainer = document.getElementById('searchResults');
      const inspCard = document.getElementById('inspectorCard');
      if (!q) return;

      if (q.includes('open.spotify.com/') || q.includes('youtube.com/') || q.includes('youtu.be/')) {
        inspectPlaylistOrLink();
        return;
      }

      inspCard.style.display = "none";
      resultsContainer.innerHTML = "<div style='color: var(--text-muted); padding: 0.5rem;'>⏳ Przeszukiwanie katalogu Spotify...</div>";
      try {
        const res = await fetch(`${API_BASE}/search?q=${encodeURIComponent(q)}&type=${type}&limit=8`);
        const data = await res.json();
        resultsContainer.innerHTML = "";

        if (data.results && data.results.length > 0) {
          data.results.forEach(item => {
            const div = document.createElement('div');
            div.className = 'track-item';
            
            if (type === 'track') {
              const durStr = formatDuration(item.duration_ms);
              const inLib = Boolean(item.in_library);
              div.innerHTML = `
                <img src="${item.cover_url || 'https://via.placeholder.com/64'}" alt="cover">
                <div class="track-info">
                  <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                    <span class="track-title">${escapeHtml(item.title)}</span>
                    ${durStr ? `<span class="badge-duration" style="font-size: 0.75rem; padding: 2px 6px;">⏱️ ${durStr}</span>` : ''}
                    ${inLib ? `<span class="badge-in-library" style="font-size: 0.75rem; padding: 2px 6px;">⚠️ Ten utwór już jest w bazie</span>` : ''}
                  </div>
                  <div class="track-artist">${escapeHtml(item.artist)} • ${escapeHtml(item.album)} (${escapeHtml(item.year || '')})</div>
                </div>
                <div style="display: flex; gap: 0.4rem; align-items: center; margin-left: auto; flex: 0 0 auto;">
                  <button class="btn-secondary" onclick="playOnlinePreview(decodeURIComponent('${encodeURIComponent(item.spotify_url || (item.artist + ' - ' + item.title))}'), decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.artist)}'))" style="padding: 0.45rem 0.75rem; font-size: 0.82rem; white-space: nowrap;" title="Odsłuchaj fragment audio przed pobraniem">
                    ▶️ Odsłuchaj
                  </button>
                  <button class="download-small-btn" onclick="startDownload('${item.spotify_url || (item.artist + ' - ' + item.title)}')" style="width: max-content; flex: 0 0 auto;">
                    ${inLib ? '⬇️ Pobierz ponownie' : '⬇️ Pobierz'}
                  </button>
                </div>
              `;
            } else if (type === 'album') {
              const artists = (item.artists || []).join(', ');
              div.innerHTML = `
                <img src="${item.cover_url || 'https://via.placeholder.com/64'}" alt="cover">
                <div class="track-info">
                  <div class="track-title">💿 ${item.name}</div>
                  <div class="track-artist">${artists} • ${item.total_tracks} utworów (${item.release_date || ''})</div>
                </div>
                <button class="download-small-btn" onclick="startDownload('${item.spotify_url}')">⬇️ Pobierz album</button>
              `;
            } else if (type === 'playlist') {
              div.innerHTML = `
                <img src="${item.cover_url || 'https://via.placeholder.com/64'}" alt="cover">
                <div class="track-info">
                  <div class="track-title">📑 ${item.name}</div>
                  <div class="track-artist">Autor: ${item.owner} • ${item.total_tracks} utworów</div>
                </div>
                <button class="download-small-btn" onclick="inspectPlaylistOrLinkExplicit('${item.spotify_url}')">📋 Wybierz utwory</button>
              `;
            }
            resultsContainer.appendChild(div);
          });
        } else {
          resultsContainer.innerHTML = "<div style='color: var(--text-muted); padding: 0.5rem;'>Brak wyników w Spotify.</div>";
        }
      } catch (err) {
        resultsContainer.innerHTML = `<div style='color: #ef4444; padding: 0.5rem;'>Błąd połączenia: ${err}</div>`;
      }
    }

    function inspectPlaylistOrLinkExplicit(url) {
      document.getElementById('queryInput').value = url;
      inspectPlaylistOrLink();
    }

    // --- CZYSZCZENIE I ODŚWIEŻANIE BIBLIOTEKI ---
    async function clearLibrary() {
      const confirmed = confirm("Czy na pewno chcesz usunąć wszystkie utwory z biblioteki i zacząć od nowa? Operacja usunie pliki z dysku i zresetuje bazę Navidrome.");
      if (!confirmed) return;

      const statusBox = document.getElementById('statusBox');
      statusBox.style.display = "block";
      statusBox.innerHTML = "⏳ Czyszczenie biblioteki muzycznej na serwerze...";
      statusBox.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch(`${API_BASE}/library/clear`, { method: 'POST' });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `✅ <b>Biblioteka została wyczyszczona!</b> Usunięto ${data.deleted_count} elementów. Navidrome ma teraz czystą bazę.`;
          document.getElementById('searchResults').innerHTML = '';
          document.getElementById('inspectorCard').style.display = 'none';
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    async function refreshNavidrome() {
      const statusBox = document.getElementById('statusBox');
      statusBox.style.display = "block";
      statusBox.innerHTML = "Wysyłanie sygnału reskanu do Navidrome...";
      statusBox.scrollIntoView({ behavior: 'smooth' });
      try {
        const res = await fetch(`${API_BASE}/refresh-navidrome`, { method: 'POST' });
        const data = await res.json();
        statusBox.innerHTML = `🔄 Sygnał reskanu wysłany: ${data.status}`;
      } catch (err) {
        statusBox.innerHTML = `Błąd: ${err}`;
      }
    }

    // =========================================================================
    // ZARZĄDZANIE ZAKŁADKAMI
    // =========================================================================
    function switchTab(tabName) {
      document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
      document.querySelectorAll('.tab-content').forEach(pane => pane.classList.remove('active'));

      const activeBtn = document.getElementById(`tabBtn-${tabName}`);
      const activePane = document.getElementById(`tab-${tabName}`);
      if (activeBtn) activeBtn.classList.add('active');
      if (activePane) activePane.classList.add('active');

      if (tabName === 'library') {
        loadLibraryTracks();
      } else if (tabName === 'playlists') {
        loadPlaylists();
      } else if (tabName === 'users') {
        loadUsers();
      }
    }

    // =========================================================================
    // MODUŁ ZARZĄDZANIA BIBLIOTEKĄ UTWORÓW
    // =========================================================================
    let allLibraryTracks = [];
    let filteredLibraryTracks = [];
    let selectedLibraryPaths = new Set();
    let currentPlayingPath = null;
    let cachedPlaylistsList = [];

    async function loadLibraryTracks(force = false) {
      const sb = document.getElementById('libraryStatusBox');
      const listEl = document.getElementById('libraryTracksList');

      if (force) {
        sb.style.display = 'block';
        sb.innerHTML = '⏳ Odświeżanie i skanowanie plików w bibliotece /music...';
      }

      try {
        const res = await fetch(`${API_BASE}/library/tracks${force ? '?force=true' : ''}`);
        const data = await res.json();
        if (!res.ok) {
          sb.style.display = 'block';
          sb.innerHTML = `❌ Błąd wczytywania biblioteki: ${data.detail || 'Nieznany błąd'}`;
          return;
        }

        sb.style.display = 'none';
        allLibraryTracks = data.tracks || [];

        // Aktualizacja liczników statystyk
        document.getElementById('statLibTracks').textContent = data.total_tracks || 0;
        document.getElementById('statLibSize').textContent = data.total_size_str || '0 B';
        document.getElementById('statLibArtists').textContent = data.total_artists || 0;
        document.getElementById('statLibAlbums').textContent = data.total_albums || 0;

        selectedLibraryPaths.clear();
        updateSelectedLibraryCounter();

        // Autocomplete dla wykonawców i albumów
        if (data.all_artists && Array.isArray(data.all_artists)) {
          const dlArt = document.getElementById('libraryArtistsDatalist');
          if (dlArt) {
            dlArt.innerHTML = data.all_artists.map(a => `<option value="${escapeHtml(a)}">`).join('');
          }
        }
        if (data.all_albums && Array.isArray(data.all_albums)) {
          const dlAlb = document.getElementById('libraryAlbumsDatalist');
          if (dlAlb) {
            dlAlb.innerHTML = data.all_albums.map(a => `<option value="${escapeHtml(a)}">`).join('');
          }
        }

        renderLibraryTracks();
      } catch (err) {
        sb.style.display = 'block';
        sb.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    function handleLibraryFilterChange() {
      renderLibraryTracks();
    }

    function renderLibraryTracks() {
      const listEl = document.getElementById('libraryTracksList');
      const searchVal = document.getElementById('librarySearchInput').value.trim().toLowerCase();
      const formatVal = document.getElementById('libraryFormatSelect').value;
      const sortVal = document.getElementById('librarySortSelect').value;

      filteredLibraryTracks = allLibraryTracks.filter(t => {
        if (formatVal !== 'ALL' && t.format !== formatVal) return false;
        if (searchVal) {
          const matchTitle = (t.title || '').toLowerCase().includes(searchVal);
          const matchArtist = (t.artist || '').toLowerCase().includes(searchVal);
          const matchAlbum = (t.album || '').toLowerCase().includes(searchVal);
          const matchFilename = (t.filename || '').toLowerCase().includes(searchVal);
          if (!matchTitle && !matchArtist && !matchAlbum && !matchFilename) return false;
        }
        return true;
      });

      // Sortowanie
      filteredLibraryTracks.sort((a, b) => {
        if (sortVal === 'newest') return (b.mtime || 0) - (a.mtime || 0);
        if (sortVal === 'oldest') return (a.mtime || 0) - (b.mtime || 0);
        if (sortVal === 'title_asc') return (a.title || '').localeCompare(b.title || '');
        if (sortVal === 'title_desc') return (b.title || '').localeCompare(a.title || '');
        if (sortVal === 'artist_asc') return (a.artist || '').localeCompare(b.artist || '');
        if (sortVal === 'size_desc') return (b.size_bytes || 0) - (a.size_bytes || 0);
        if (sortVal === 'duration_desc') {
          return parseDurationStr(b.duration_str) - parseDurationStr(a.duration_str);
        }
        return 0;
      });

      document.getElementById('libraryFilterCountInfo').textContent = `Widoczne: ${filteredLibraryTracks.length} / ${allLibraryTracks.length}`;

      if (filteredLibraryTracks.length === 0) {
        if (allLibraryTracks.length === 0) {
          listEl.innerHTML = `
            <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-muted);">
              <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">🎵</div>
              <div style="font-weight: 700; font-size: 1.1rem; color: #fff; margin-bottom: 0.3rem;">Brak utworów w bibliotece</div>
              <div>Przejdź do zakładki <b>Pobieranie</b>, aby dodać pierwsze utwory lub całe playlisty!</div>
            </div>
          `;
        } else {
          listEl.innerHTML = `
            <div style="text-align: center; padding: 2rem; color: var(--text-muted);">
              🔍 Brak utworów pasujących do filtra „${escapeHtml(searchVal)}”.
            </div>
          `;
        }
        return;
      }

      listEl.innerHTML = filteredLibraryTracks.map((t, idx) => {
        const isChecked = selectedLibraryPaths.has(t.rel_path);
        const isPlaying = currentPlayingPath === t.rel_path;
        const fmtClass = t.format === 'OPUS' ? 'badge-fmt-opus' : (t.format === 'MP3' ? 'badge-fmt-mp3' : (t.format === 'FLAC' ? 'badge-fmt-flac' : (t.format === 'M4A' ? 'badge-fmt-m4a' : 'badge-fmt')));
        const safeTitle = escapeHtml(t.title || t.filename);
        const safeArtist = escapeHtml(t.artist || 'Nieznany wykonawca');
        const safeAlbum = escapeHtml(t.album || 'Brak albumu');

        return `
          <div class="track-lib-item ${isPlaying ? 'active-playing' : ''}" id="lib-track-${idx}">
            <input type="checkbox" style="width: auto; cursor: pointer; transform: scale(1.15);"
              ${isChecked ? 'checked' : ''}
              onchange="toggleLibraryTrackSelectByIndex(${idx}, this.checked)">

            <img src="${API_BASE}/library/cover?path=${encodeURIComponent(t.rel_path)}" alt="cover" style="width: 50px; height: 50px; border-radius: 6px; object-fit: cover; background: #0f172a; border: 1px solid var(--border); flex-shrink: 0;" onerror="this.onerror=null; this.src='https://via.placeholder.com/50?text=♪';">

            <div style="flex: 1; min-width: 180px;">
              <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                <span class="${fmtClass}" style="font-size: 0.7rem; font-weight: 700; padding: 2px 6px; border-radius: 4px;">${t.format}</span>
                <span style="font-weight: 700; font-size: 0.95rem; color: #fff;">${safeTitle}</span>
              </div>
              <div style="color: var(--text-muted); font-size: 0.82rem; margin-top: 0.2rem;">
                🎤 <span style="color: #cbd5e1;">${safeArtist}</span> • 💿 <span>${safeAlbum}</span>
              </div>
              <div style="color: #64748b; font-size: 0.74rem; margin-top: 0.15rem; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 500px;" title="${escapeHtml(t.rel_path)}">
                📁 ${escapeHtml(t.rel_path)}
              </div>
            </div>

            <div style="display: flex; align-items: center; gap: 0.8rem; font-size: 0.82rem; color: var(--text-muted); flex-wrap: wrap;">
              ${t.duration_str ? `<span>⏱️ ${t.duration_str}</span>` : ''}
              ${t.bitrate_kbps ? `<span>⚡ ${t.bitrate_kbps} kbps</span>` : ''}
              <span>💾 ${t.size_str}</span>
            </div>

            <div style="display: flex; gap: 0.4rem; align-items: center; flex: 0 0 auto; flex-wrap: wrap;">
              <button class="btn-secondary" style="padding: 0.4rem 0.75rem; font-size: 0.8rem;" onclick="playLibraryTrackByIndex(${idx})" title="Odsłuchaj w przeglądarce">
                ${isPlaying ? '⏸️ Pauza' : '▶️ Odsłuchaj'}
              </button>
              <button class="btn-secondary" style="padding: 0.4rem 0.75rem; font-size: 0.8rem;" onclick="openEditMetadataModalByIndex(${idx})" title="Edytuj metadane utworu (tytuł, wykonawca, album)">
                ✏️ Edytuj
              </button>
              <button class="btn-secondary" style="padding: 0.4rem 0.75rem; font-size: 0.8rem;" onclick="openReplaceTrackModalByIndex(${idx})" title="Podmień utwór na inną wersję">
                🔄 Podmień
              </button>
              <button class="btn-secondary" style="padding: 0.4rem 0.75rem; font-size: 0.8rem;" onclick="openAddToPlaylistModalByIndex(${idx})" title="Dodaj do playlisty Navidrome">
                ➕ Do playlisty
              </button>
              <button class="btn-secondary btn-danger-soft" style="padding: 0.4rem 0.65rem; font-size: 0.8rem;" onclick="deleteSingleLibraryTrackByIndex(${idx})" title="Usuń utwór z dysku i biblioteki">
                🗑️
              </button>
            </div>
          </div>
        `;
      }).join('');
    }

    function parseDurationStr(str) {
      if (!str) return 0;
      const parts = str.split(':');
      if (parts.length === 2) return parseInt(parts[0]) * 60 + parseInt(parts[1]);
      if (parts.length === 3) return parseInt(parts[0]) * 3600 + parseInt(parts[1]) * 60 + parseInt(parts[2]);
      return 0;
    }

    function toggleLibraryTrackSelectByIndex(idx, checked) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;
      if (checked) {
        selectedLibraryPaths.add(track.rel_path);
      } else {
        selectedLibraryPaths.delete(track.rel_path);
      }
      updateSelectedLibraryCounter();
    }

    function toggleSelectAllLibrary(check) {
      filteredLibraryTracks.forEach(t => {
        if (check) {
          selectedLibraryPaths.add(t.rel_path);
        } else {
          selectedLibraryPaths.delete(t.rel_path);
        }
      });
      updateSelectedLibraryCounter();
      renderLibraryTracks();
    }

    function updateSelectedLibraryCounter() {
      const count = selectedLibraryPaths.size;
      const btn = document.getElementById('btnDeleteSelectedLib');
      const counter = document.getElementById('selectedLibCount');
      if (counter) counter.textContent = count;
      if (btn) {
        btn.style.display = count > 0 ? 'inline-block' : 'none';
      }
    }

    async function deleteSingleLibraryTrackByIndex(idx) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;
      const title = track.title || track.filename;
      if (!confirm(`Czy na pewno usunąć utwór "${title}" z dysku i biblioteki?`)) return;

      try {
        const res = await fetch(`${API_BASE}/library/tracks`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: [track.rel_path] })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`🗑️ Usunięto utwór <b>${escapeHtml(title)}</b>.`);
          if (currentPlayingPath === track.rel_path) closeAudioPlayer();
          loadLibraryTracks();
        } else {
          alert(`Błąd usuwania: ${data.detail || 'Nieznany błąd'}`);
        }
      } catch (err) {
        alert(`Błąd połączenia: ${err}`);
      }
    }

    async function deleteSelectedLibraryTracks() {
      const count = selectedLibraryPaths.size;
      if (count === 0) return;
      if (!confirm(`Czy na pewno bezpowrotnie usunąć ${count} zaznaczonych utworów z dysku?`)) return;

      const paths = Array.from(selectedLibraryPaths);
      try {
        const res = await fetch(`${API_BASE}/library/tracks`, {
          method: 'DELETE',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: paths })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`🗑️ Pomyślnie usunięto ${data.deleted_count} utworów (zwolniono ${data.freed_str}).`);
          selectedLibraryPaths.clear();
          loadLibraryTracks();
        } else {
          alert(`Błąd usuwania: ${data.detail || 'Nieznany błąd'}`);
        }
      } catch (err) {
        alert(`Błąd połączenia: ${err}`);
      }
    }

    function playLibraryTrackByIndex(idx) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;

      const playerBar = document.getElementById('audioPlayerBar');
      const audio = document.getElementById('globalAudioPlayer');
      const titleEl = document.getElementById('playerTrackTitle');
      const artistEl = document.getElementById('playerTrackArtist');

      if (currentPlayingPath === track.rel_path && !audio.paused) {
        audio.pause();
        currentPlayingPath = null;
        renderLibraryTracks();
        return;
      }

      currentPlayingPath = track.rel_path;
      titleEl.textContent = track.title || track.filename;
      artistEl.textContent = track.artist ? `${track.artist} • ${track.album} (${track.format})` : track.format;

      audio.src = `${API_BASE}/library/stream?path=${encodeURIComponent(track.rel_path)}`;
      playerBar.style.display = 'block';
      audio.play().catch(e => console.log('Autoplay prevented:', e));

      renderLibraryTracks();
    }

    function closeAudioPlayer() {
      const playerBar = document.getElementById('audioPlayerBar');
      const audio = document.getElementById('globalAudioPlayer');
      if (audio) {
        audio.pause();
        audio.src = '';
      }
      currentPlayingPath = null;
      if (playerBar) playerBar.style.display = 'none';
      renderLibraryTracks();
    }

    let targetModalTrack = null;

    async function openAddToPlaylistModalByIndex(idx) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;

      targetModalTrack = track;
      document.getElementById('modalTrackTitle').textContent = track.title || track.filename;
      document.getElementById('modalTrackArtist').textContent = track.artist || 'Nieznany wykonawca';

      const select = document.getElementById('modalPlaylistSelect');
      select.innerHTML = '<option value="" disabled selected>⏳ Pobieranie playlist z Navidrome...</option>';
      document.getElementById('addToPlaylistModal').style.display = 'flex';

      try {
        const res = await fetch(`${API_BASE}/library/playlists`);
        const playlists = await res.json();
        if (!res.ok || !Array.isArray(playlists) || playlists.length === 0) {
          select.innerHTML = '<option value="" disabled selected>Brak playlist w Navidrome. Stwórz najpierw playlistę w zakładce Playlisty.</option>';
          return;
        }

        cachedPlaylistsList = playlists;
        select.innerHTML = playlists.map(pl => {
          return `<option value="${escapeHtml(pl.id)}">${escapeHtml(pl.name)} (${pl.songCount || 0} utworów)</option>`;
        }).join('');
      } catch (err) {
        select.innerHTML = `<option value="" disabled selected>Błąd pobierania playlist: ${err}</option>`;
      }
    }

    function closeAddToPlaylistModal() {
      document.getElementById('addToPlaylistModal').style.display = 'none';
      targetModalTrack = null;
    }

    async function submitAddToPlaylist() {
      const select = document.getElementById('modalPlaylistSelect');
      const playlistId = select.value;
      if (!playlistId || !targetModalTrack) {
        alert("Wybierz playlistę!");
        return;
      }

      const btn = document.getElementById('btnSubmitAddToPlaylist');
      btn.disabled = true;
      btn.textContent = '⏳ Dodawanie...';

      try {
        const res = await fetch(`${API_BASE}/library/playlists/${playlistId}/add-track`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            title: targetModalTrack.title || targetModalTrack.filename,
            artist: targetModalTrack.artist || '',
            rel_path: targetModalTrack.rel_path
          })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`🎶 Dodano <b>${escapeHtml(targetModalTrack.title || targetModalTrack.filename)}</b> do playlisty!`);
          closeAddToPlaylistModal();
        } else {
          alert(`Błąd: ${data.detail || 'Nie udało się dodać utworu do playlisty'}`);
        }
      } catch (err) {
        alert(`Błąd połączenia: ${err}`);
      } finally {
        btn.disabled = false;
        btn.textContent = '➕ Dodaj';
      }
    }

    // =========================================================================
    // MODUŁ DUPLIKATÓW
    // =========================================================================
    let currentDuplicateGroups = [];

    async function scanDuplicates() {
      const sb = document.getElementById('duplicatesStatusBox');
      const listEl = document.getElementById('duplicatesList');
      const banner = document.getElementById('duplicatesStatsBanner');

      sb.style.display = 'block';
      sb.innerHTML = '⏳ Skanowanie plików audio w katalogu /music i wykrywanie duplikatów po tytule...';
      banner.style.display = 'none';
      listEl.innerHTML = '';

      try {
        const res = await fetch(`${API_BASE}/library/duplicates`);
        const data = await res.json();
        if (!res.ok) {
          sb.innerHTML = `❌ Błąd skanowania: ${data.detail || 'Nieznany błąd'}`;
          return;
        }

        currentDuplicateGroups = data.groups || [];
        sb.style.display = 'none';

        if (currentDuplicateGroups.length === 0) {
          listEl.innerHTML = `
            <div style="text-align: center; padding: 2.5rem 1rem; color: var(--text-muted);">
              <div style="font-size: 2.5rem; margin-bottom: 0.5rem;">✨</div>
              <div style="font-weight: 700; font-size: 1.1rem; color: #fff; margin-bottom: 0.3rem;">Brak duplikatów!</div>
              <div>Twoja biblioteka jest czysta – każdy utwór występuje tylko raz.</div>
            </div>
          `;
          return;
        }

        document.getElementById('statDupGroups').innerText = data.total_duplicate_groups;
        document.getElementById('statDupFiles').innerText = data.total_duplicate_files;
        document.getElementById('statWastedSpace').innerText = data.potential_wasted_str;
        banner.style.display = 'block';

        renderDuplicatesList();
        selectNonFirstDuplicates();

      } catch (e) {
        sb.innerHTML = `❌ Błąd połączenia: ${e}`;
      }
    }

    function renderDuplicatesList() {
      const listEl = document.getElementById('duplicatesList');
      listEl.innerHTML = '';

      currentDuplicateGroups.forEach((group, gIdx) => {
        const groupDiv = document.createElement('div');
        groupDiv.className = 'dup-group';
        groupDiv.id = `dup-group-${gIdx}`;

        groupDiv.innerHTML = `
          <div class="dup-group-header">
            <div>
              <span style="font-weight: 700; color: #fff; font-size: 0.95rem;">🎵 ${escapeHtml(group.display_title)}</span>
              <span style="color: var(--text-muted); font-size: 0.8rem; margin-left: 0.5rem;">(${group.count} plików w bibliotece)</span>
            </div>
            <div style="display: flex; gap: 0.4rem;">
              <button class="btn-secondary" style="padding: 0.25rem 0.6rem; font-size: 0.75rem;" onclick="selectGroupNonFirst(${gIdx})">Zaznacz gorsze</button>
            </div>
          </div>
          <div>
            ${group.files.map((f, fIdx) => `
              <div class="dup-item" id="dup-item-${gIdx}-${fIdx}">
                <input type="checkbox" class="dup-checkbox" id="dup-check-${gIdx}-${fIdx}" data-path="${escapeHtml(f.rel_path)}" onchange="updateSelectedDupCounter()" style="width: 18px; height: 18px; cursor: pointer;">
                <span class="badge-fmt">${escapeHtml(f.format)}</span>
                <span style="font-size: 0.78rem; color: #38bdf8; font-weight: 600;">${escapeHtml(f.size_str)}</span>
                ${f.duration_str ? `<span style="font-size: 0.75rem; color: var(--text-muted);">⏱️ ${f.duration_str}</span>` : ''}
                <div style="flex: 1; min-width: 200px;">
                  <div style="font-size: 0.88rem; font-weight: 600; color: #fff;">${escapeHtml(f.artist)} • <span style="color: #cbd5e1;">${escapeHtml(f.album)}</span></div>
                  <div style="font-size: 0.75rem; color: var(--text-muted); word-break: break-all;">📁 ${escapeHtml(f.rel_path)}</div>
                </div>
                <button class="btn-secondary btn-danger-soft" style="flex: 0 0 auto; width: auto; padding: 0.35rem 0.7rem; font-size: 0.78rem;" onclick="deleteSingleDuplicate('${encodeURIComponent(f.rel_path)}', ${gIdx}, ${fIdx})">
                  🗑️ Usuń
                </button>
              </div>
            `).join('')}
          </div>
        `;
        listEl.appendChild(groupDiv);
      });

      updateSelectedDupCounter();
    }

    function selectNonFirstDuplicates() {
      currentDuplicateGroups.forEach((_, gIdx) => {
        selectGroupNonFirst(gIdx);
      });
      updateSelectedDupCounter();
    }

    function selectGroupNonFirst(gIdx) {
      const group = currentDuplicateGroups[gIdx];
      if (!group) return;
      group.files.forEach((_, fIdx) => {
        const cb = document.getElementById(`dup-check-${gIdx}-${fIdx}`);
        if (cb) cb.checked = (fIdx > 0);
      });
    }

    function toggleAllDuplicates(checked) {
      document.querySelectorAll('.dup-checkbox').forEach(cb => cb.checked = checked);
      updateSelectedDupCounter();
    }

    function updateSelectedDupCounter() {
      let count = 0;
      document.querySelectorAll('.dup-checkbox').forEach(cb => {
        if (cb.checked) count++;
      });
      const el = document.getElementById('selectedDupFilesCount');
      if (el) el.innerText = count;
    }

    async function deleteSelectedDuplicates() {
      const selectedPaths = [];
      document.querySelectorAll('.dup-checkbox').forEach(cb => {
        if (cb.checked) {
          selectedPaths.push(cb.getAttribute('data-path'));
        }
      });

      if (selectedPaths.length === 0) {
        alert("Zaznacz co najmniej jeden plik do usunięcia!");
        return;
      }

      if (!confirm(`Czy na pewno chcesz trwale usunąć ${selectedPaths.length} zaznaczonych plików z dysku?`)) {
        return;
      }

      showToast(`⏳ Usuwanie ${selectedPaths.length} plików...`);

      try {
        const res = await fetch(`${API_BASE}/library/duplicates/delete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: selectedPaths })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`✅ Usunięto ${data.deleted_count} plików (zwolniono ${data.freed_str})!`);
          scanDuplicates();
        } else {
          alert(`Błąd: ${data.detail || 'Nie udało się usunąć plików'}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    async function deleteSingleDuplicate(encRelPath, gIdx, fIdx) {
      const relPath = decodeURIComponent(encRelPath);
      if (!confirm(`Czy na pewno usunąć ten plik z dysku?\n${relPath}`)) return;

      try {
        const res = await fetch(`${API_BASE}/library/duplicates/delete`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ paths: [relPath] })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`✅ Usunięto plik (zwolniono ${data.freed_str})!`);
          const row = document.getElementById(`dup-item-${gIdx}-${fIdx}`);
          if (row) row.style.display = 'none';
          updateSelectedDupCounter();
        } else {
          alert(`Błąd: ${data.detail}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    // =========================================================================
    // MODUŁ PLAYLIST (NAVIDROME SUBSONIC)
    // =========================================================================
    let openPlaylistId = null;

    async function loadPlaylists() {
      const sb = document.getElementById('playlistsStatusBox');
      const listEl = document.getElementById('playlistsList');
      sb.style.display = 'block';
      sb.innerHTML = '⏳ Pobieranie playlist z Navidrome...';

      try {
        const res = await fetch(`${API_BASE}/library/playlists`);
        const pls = await res.json();
        sb.style.display = 'none';

        if (!Array.isArray(pls) || pls.length === 0) {
          listEl.innerHTML = `<div style="grid-column: 1 / -1; color: var(--text-muted); text-align: center; padding: 2rem;">Brak playlist w Navidrome. Utwórz pierwszą powyższym przyciskiem!</div>`;
          return;
        }

        listEl.innerHTML = pls.map(pl => {
          const encId = encodeURIComponent(pl.id);
          const encName = encodeURIComponent(pl.name);
          return `
            <div class="card" style="margin-bottom: 0; background: #0f172a; border-color: var(--border);">
              <div style="font-weight: 700; font-size: 1.05rem; color: #fff; margin-bottom: 0.3rem;">📑 ${escapeHtml(pl.name)}</div>
              <div style="color: var(--text-muted); font-size: 0.8rem; margin-bottom: 0.8rem;">
                ${pl.songCount || 0} utworów • ⏱️ ${Math.round((pl.duration || 0) / 60)} min
              </div>
              <div style="display: flex; gap: 0.5rem;">
                <button class="btn-primary" style="padding: 0.45rem 0.8rem; font-size: 0.82rem;" onclick="openPlaylistDetail(decodeURIComponent('${encId}'), decodeURIComponent('${encName}'))">
                  📂 Zobacz i dodaj
                </button>
                <button class="btn-secondary btn-danger-soft" style="flex: 0 0 auto; width: auto; padding: 0.45rem 0.8rem; font-size: 0.82rem;" onclick="deletePlaylist(decodeURIComponent('${encId}'), decodeURIComponent('${encName}'))">
                  🗑️
                </button>
              </div>
            </div>
          `;
        }).join('');
      } catch (e) {
        sb.innerHTML = `❌ Błąd: ${e}`;
      }
    }

    function toggleCreatePlaylistBox(show = null) {
      const box = document.getElementById('createPlaylistBox');
      if (show === null) {
        box.style.display = box.style.display === 'block' ? 'none' : 'block';
      } else {
        box.style.display = show ? 'block' : 'none';
      }
      if (box.style.display === 'block') {
        document.getElementById('newPlaylistName').focus();
      }
    }

    async function submitCreatePlaylist() {
      const name = document.getElementById('newPlaylistName').value.trim();
      if (!name) {
        alert("Wpisz nazwę playlisty!");
        return;
      }
      try {
        const res = await fetch(`${API_BASE}/library/playlists`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ name: name })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`✅ Utworzono playlistę: <b>${escapeHtml(name)}</b>`);
          document.getElementById('newPlaylistName').value = '';
          toggleCreatePlaylistBox(false);
          loadPlaylists();
        } else {
          alert(`Błąd: ${data.detail || 'Nie udało się utworzyć playlisty'}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    async function deletePlaylist(plId, plName) {
      if (!confirm(`Czy na pewno chcesz usunąć playlistę "${plName}"?`)) return;
      try {
        const res = await fetch(`${API_BASE}/library/playlists/${plId}`, { method: 'DELETE' });
        if (res.ok) {
          showToast(`🗑️ Usunięto playlistę ${escapeHtml(plName)}`);
          if (openPlaylistId === plId) closePlaylistDetail();
          loadPlaylists();
        } else {
          alert("Nie udało się usunąć playlisty.");
        }
      } catch (e) {
        alert(`Błąd: ${e}`);
      }
    }

    async function openPlaylistDetail(plId, plName) {
      openPlaylistId = plId;
      const box = document.getElementById('activePlaylistDetailBox');
      box.style.display = 'block';
      document.getElementById('activePlaylistNameHeader').innerText = plName;
      document.getElementById('activePlaylistSubheader').innerText = 'Ładowanie utworów...';
      document.getElementById('activePlaylistTracksList').innerHTML = '';
      document.getElementById('searchSongsResults').innerHTML = '';
      box.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch(`${API_BASE}/library/playlists/${plId}`);
        const data = await res.json();
        const tracks = data.tracks || [];
        document.getElementById('activePlaylistSubheader').innerText = `${tracks.length} utworów w playliście`;

        if (tracks.length === 0) {
          document.getElementById('activePlaylistTracksList').innerHTML = `<div style="color: var(--text-muted); font-size: 0.85rem; padding: 0.6rem;">Playlista jest pusta. Wyszukaj utwory powyżej i dodaj je!</div>`;
          return;
        }

        document.getElementById('activePlaylistTracksList').innerHTML = tracks.map((t, idx) => `
          <div style="display: flex; align-items: center; justify-content: space-between; padding: 0.5rem 0.8rem; background: #0f172a; border-radius: 6px; border: 1px solid var(--border);">
            <div style="display: flex; align-items: center; gap: 0.6rem; min-width: 0;">
              <span style="color: var(--text-muted); font-size: 0.8rem; width: 24px;">#${idx + 1}</span>
              <div style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
                <span style="font-weight: 600; font-size: 0.88rem; color: #fff;">${escapeHtml(t.title)}</span>
                <span style="font-size: 0.78rem; color: var(--text-muted);"> • ${escapeHtml(t.artist)}</span>
              </div>
            </div>
            <div style="display: flex; align-items: center; gap: 0.6rem; flex-shrink: 0;">
              <span style="font-size: 0.78rem; color: #38bdf8;">⏱️ ${formatDuration((t.duration || 0) * 1000)}</span>
              <button class="btn-secondary btn-danger-soft" style="width: auto; padding: 0.25rem 0.55rem; font-size: 0.75rem;" onclick="removeTrackFromPlaylist('${plId}', ${idx})">❌</button>
            </div>
          </div>
        `).join('');
      } catch (e) {
        document.getElementById('activePlaylistSubheader').innerText = `Błąd: ${e}`;
      }
    }

    function closePlaylistDetail() {
      openPlaylistId = null;
      document.getElementById('activePlaylistDetailBox').style.display = 'none';
    }

    async function removeTrackFromPlaylist(plId, idx) {
      try {
        const res = await fetch(`${API_BASE}/library/playlists/${plId}/tracks/${idx}`, { method: 'DELETE' });
        if (res.ok) {
          showToast("Usunięto utwór z playlisty.");
          openPlaylistDetail(plId, document.getElementById('activePlaylistNameHeader').innerText);
        }
      } catch (e) {
        alert(`Błąd: ${e}`);
      }
    }

    async function searchSongsForPlaylist() {
      const q = document.getElementById('searchSongToAddInput').value.trim();
      const resContainer = document.getElementById('searchSongsResults');
      if (!q) return;

      resContainer.innerHTML = '<div style="color: var(--text-muted); font-size: 0.8rem;">⏳ Wyszukiwanie w Navidrome...</div>';

      try {
        const res = await fetch(`${API_BASE}/library/songs/search?q=${encodeURIComponent(q)}`);
        const songs = await res.json();
        if (!Array.isArray(songs) || songs.length === 0) {
          resContainer.innerHTML = '<div style="color: #f59e0b; font-size: 0.8rem;">Brak wyników w bazie.</div>';
          return;
        }

        resContainer.innerHTML = songs.map(s => `
          <div style="display: flex; align-items: center; justify-content: space-between; padding: 0.45rem 0.7rem; background: #0f172a; border-radius: 6px; border: 1px solid var(--border);">
            <div style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">
              <span style="font-weight: 600; font-size: 0.85rem; color: #fff;">${escapeHtml(s.title)}</span>
              <span style="font-size: 0.78rem; color: var(--text-muted);"> • ${escapeHtml(s.artist)}</span>
            </div>
            <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0.3rem 0.7rem; font-size: 0.78rem;" onclick="addSongToActivePlaylist('${s.id}')">
              ➕ Dodaj
            </button>
          </div>
        `).join('');
      } catch (e) {
        resContainer.innerHTML = `<div style="color: #ef4444; font-size: 0.8rem;">Błąd: ${e}</div>`;
      }
    }

    async function addSongToActivePlaylist(songId) {
      if (!openPlaylistId) return;
      try {
        const res = await fetch(`${API_BASE}/library/playlists/${openPlaylistId}/tracks`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ song_ids: [songId] })
        });
        if (res.ok) {
          showToast("✅ Dodano utwór do playlisty!");
          openPlaylistDetail(openPlaylistId, document.getElementById('activePlaylistNameHeader').innerText);
        } else {
          alert("Nie udało się dodać utworu.");
        }
      } catch (e) {
        alert(`Błąd: ${e}`);
      }
    }

    // =========================================================================
    // MODUŁ UŻYTKOWNIKÓW (NAVIDROME NATIVE API)
    // =========================================================================
    async function loadUsers() {
      const sb = document.getElementById('usersStatusBox');
      const container = document.getElementById('usersListContainer');
      sb.style.display = 'block';
      sb.innerHTML = '⏳ Pobieranie użytkowników z Navidrome...';

      try {
        const res = await fetch(`${API_BASE}/library/users`);
        const users = await res.json();
        sb.style.display = 'none';

        if (!Array.isArray(users) || users.length === 0) {
          container.innerHTML = `
            <div style="background: rgba(234, 179, 8, 0.1); border: 1px solid rgba(234, 179, 8, 0.3); border-radius: 8px; padding: 1rem; color: #fde047; font-size: 0.88rem;">
              ⚠️ Brak dostępu do kont lub nie skonfigurowano hasła administratora w <code>NAVIDROME_ADMIN_PASSWORD</code> w pliku <code>.env</code>. Uzupełnij hasło, zrestartuj kontener i spróbuj ponownie.
            </div>
          `;
          return;
        }

        container.innerHTML = `
          <table class="data-table">
            <thead>
              <tr>
                <th>Użytkownik</th>
                <th>Rola</th>
                <th>E-mail</th>
                <th style="text-align: right;">Akcje</th>
              </tr>
            </thead>
            <tbody>
              ${users.map(u => {
                const isAdmin = Boolean(u.isAdmin);
                const encId = encodeURIComponent(u.id);
                const encUser = encodeURIComponent(u.userName);
                return `
                  <tr>
                    <td>
                      <div style="font-weight: 700; color: #fff;">${escapeHtml(u.userName)}</div>
                      <div style="color: var(--text-muted); font-size: 0.8rem;">${escapeHtml(u.name || '')}</div>
                    </td>
                    <td>
                      <span class="${isAdmin ? 'badge-admin' : 'badge-user'}">
                        ${isAdmin ? '👑 Administrator' : '👤 Użytkownik'}
                      </span>
                    </td>
                    <td style="color: var(--text-muted); font-size: 0.82rem;">${escapeHtml(u.email || '—')}</td>
                    <td style="text-align: right;">
                      <div style="display: inline-flex; gap: 0.4rem;">
                        <button class="btn-secondary" style="padding: 0.35rem 0.65rem; font-size: 0.78rem;" onclick="promptChangePassword(decodeURIComponent('${encId}'), decodeURIComponent('${encUser}'))" title="Zmień hasło">
                          🔑 Hasło
                        </button>
                        <button class="btn-secondary" style="padding: 0.35rem 0.65rem; font-size: 0.78rem;" onclick="toggleUserAdmin(decodeURIComponent('${encId}'), decodeURIComponent('${encUser}'), ${isAdmin})" title="${isAdmin ? 'Zmień na zwykłego użytkownika' : 'Nadaj uprawnienia administratora'}">
                          ${isAdmin ? '👤 Odbierz admina' : '👑 Nadaj admina'}
                        </button>
                        <button class="btn-secondary btn-danger-soft" style="padding: 0.35rem 0.65rem; font-size: 0.78rem;" onclick="deleteUser(decodeURIComponent('${encId}'), decodeURIComponent('${encUser}'))" title="Usuń konto">
                          🗑️
                        </button>
                      </div>
                    </td>
                  </tr>
                `;
              }).join('')}
            </tbody>
          </table>
        `;
      } catch (e) {
        sb.innerHTML = `❌ Błąd: ${e}`;
      }
    }

    function toggleCreateUserBox(show = null) {
      const box = document.getElementById('createUserBox');
      if (show === null) {
        box.style.display = box.style.display === 'block' ? 'none' : 'block';
      } else {
        box.style.display = show ? 'block' : 'none';
      }
    }

    async function submitCreateUser() {
      const username = document.getElementById('newUsername').value.trim();
      const name = document.getElementById('newName').value.trim();
      const password = document.getElementById('newPassword').value;
      const email = document.getElementById('newEmail').value.trim();
      const isAdmin = document.getElementById('newIsAdmin').checked;

      if (!username || !password) {
        alert("Podaj login i hasło!");
        return;
      }

      try {
        const res = await fetch(`${API_BASE}/library/users`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            username: username,
            name: name,
            password: password,
            email: email,
            is_admin: isAdmin
          })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`✅ Utworzono użytkownika <b>${escapeHtml(username)}</b>!`);
          document.getElementById('newUsername').value = '';
          document.getElementById('newName').value = '';
          document.getElementById('newPassword').value = '';
          document.getElementById('newEmail').value = '';
          document.getElementById('newIsAdmin').checked = false;
          toggleCreateUserBox(false);
          loadUsers();
        } else {
          alert(`Błąd: ${data.detail || 'Nie udało się utworzyć użytkownika'}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    async function promptChangePassword(userId, userName) {
      const newPass = prompt(`Wpisz nowe hasło dla użytkownika "${userName}":`);
      if (!newPass) return;

      try {
        const res = await fetch(`${API_BASE}/library/users/${userId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ password: newPass })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`🔑 Zmieniono hasło dla <b>${escapeHtml(userName)}</b>!`);
        } else {
          alert(`Błąd: ${data.detail || 'Nie udało się zmienić hasła'}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    async function toggleUserAdmin(userId, userName, currentIsAdmin) {
      const targetAdmin = !currentIsAdmin;
      const msg = targetAdmin
        ? `Czy na pewno nadać uprawnienia administratora użytkownikowi "${userName}"?`
        : `Czy na pewno odebrać uprawnienia administratora użytkownikowi "${userName}"?`;
      if (!confirm(msg)) return;

      try {
        const res = await fetch(`${API_BASE}/library/users/${userId}`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ is_admin: targetAdmin })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`Zaktualizowano rolę dla <b>${escapeHtml(userName)}</b>`);
          loadUsers();
        } else {
          alert(`Błąd: ${data.detail}`);
        }
      } catch (e) {
        alert(`Błąd połączenia: ${e}`);
      }
    }

    async function deleteUser(userId, userName) {
      if (!confirm(`Czy na pewno bezpowrotnie usunąć konto użytkownika "${userName}"?`)) return;
      try {
        const res = await fetch(`${API_BASE}/library/users/${userId}`, { method: 'DELETE' });
        if (res.ok) {
          showToast(`🗑️ Usunięto użytkownika ${escapeHtml(userName)}`);
          loadUsers();
        } else {
          alert("Nie udało się usunąć użytkownika.");
        }
      } catch (e) {
        alert(`Błąd: ${e}`);
      }
    }

    // --- ODSŁUCH AUDIO ONLINE (PREVIEW) ---
    function playOnlinePreview(queryOrUrl, title, artist) {
      if (!queryOrUrl) return;
      const playerBar = document.getElementById('audioPlayerBar');
      const audio = document.getElementById('globalAudioPlayer');
      const titleEl = document.getElementById('playerTrackTitle');
      const artistEl = document.getElementById('playerTrackArtist');

      currentPlayingPath = null;
      titleEl.textContent = title || 'Podgląd audio';
      artistEl.textContent = (artist || '') + ' (Streaming online)';

      audio.src = `${API_BASE}/preview/audio?q=${encodeURIComponent(queryOrUrl)}`;
      playerBar.style.display = 'block';
      audio.play().catch(e => console.log('Autoplay prevented:', e));

      renderLibraryTracks();
    }

    // --- EDYCJA METADANYCH UTWORU ---
    let currentEditTrack = null;

    function openEditMetadataModalByIndex(idx) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;
      currentEditTrack = track;

      document.getElementById('editModalCover').src = `${API_BASE}/library/cover?path=${encodeURIComponent(track.rel_path)}`;
      document.getElementById('editModalOriginalPath').textContent = track.rel_path;
      document.getElementById('editTrackTitle').value = track.title || '';
      document.getElementById('editTrackArtist').value = track.artist || '';
      document.getElementById('editTrackAlbum').value = track.album || '';

      document.getElementById('editMetadataModal').style.display = 'flex';
    }

    function closeEditMetadataModal() {
      document.getElementById('editMetadataModal').style.display = 'none';
      currentEditTrack = null;
    }

    async function submitSaveMetadata() {
      if (!currentEditTrack) return;
      const title = document.getElementById('editTrackTitle').value.trim();
      const artist = document.getElementById('editTrackArtist').value.trim();
      const album = document.getElementById('editTrackAlbum').value.trim();

      const btn = document.getElementById('btnSaveMetadata');
      btn.disabled = true;
      btn.textContent = '⏳ Zapisywanie...';

      try {
        const res = await fetch(`${API_BASE}/library/tracks/metadata`, {
          method: 'PUT',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            rel_path: currentEditTrack.rel_path,
            title: title,
            artist: artist,
            album: album
          })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`💾 Zaktualizowano metadane: <b>${escapeHtml(title || currentEditTrack.filename)}</b>`);
          closeEditMetadataModal();
          loadLibraryTracks();
        } else {
          alert(`Błąd zapisu metadanych: ${data.detail || 'Nieznany błąd'}`);
        }
      } catch (err) {
        alert(`Błąd połączenia: ${err}`);
      } finally {
        btn.disabled = false;
        btn.textContent = '💾 Zapisz zmiany';
      }
    }

    // --- PODMIANA UTWORU (REPLACE TRACK) ---
    let currentReplaceTrack = null;
    let replaceSearchMode = 'spotify';

    function openReplaceTrackModalByIndex(idx) {
      const track = filteredLibraryTracks[idx];
      if (!track) return;
      currentReplaceTrack = track;

      document.getElementById('replaceModalCurrentCover').src = `${API_BASE}/library/cover?path=${encodeURIComponent(track.rel_path)}`;
      document.getElementById('replaceModalCurrentTitle').textContent = track.title || track.filename;
      document.getElementById('replaceModalCurrentArtist').textContent = track.artist || 'Nieznany wykonawca';
      document.getElementById('replaceModalCurrentMeta').textContent = `${track.album || 'Brak albumu'} • ${track.format || ''} • ${track.duration_str || ''}`;

      const defaultQuery = `${track.artist || ''} ${track.title || ''}`.trim() || track.title || track.filename;
      document.getElementById('replaceSearchInput').value = defaultQuery;

      switchReplaceSearchMode('spotify', false);
      document.getElementById('replaceSearchResults').innerHTML = `
        <div style="text-align: center; padding: 2rem; color: var(--text-muted); font-size: 0.85rem;">
          Wpisz tytuł lub wykonawcę i kliknij „🔍 Szukaj”, aby znaleźć alternatywne wersje.
        </div>
      `;
      document.getElementById('replaceSearchStatus').style.display = 'none';
      document.getElementById('replaceTrackModal').style.display = 'flex';

      if (defaultQuery) {
        searchReplaceVersions();
      }
    }

    function closeReplaceTrackModal() {
      document.getElementById('replaceTrackModal').style.display = 'none';
      currentReplaceTrack = null;
    }

    function switchReplaceSearchMode(mode, doSearch = true) {
      replaceSearchMode = mode;
      const btnSpo = document.getElementById('btnReplaceTabSpotify');
      const btnYt = document.getElementById('btnReplaceTabYouTube');

      if (mode === 'spotify') {
        btnSpo.style.background = '#1e293b';
        btnSpo.style.borderColor = 'var(--primary)';
        btnSpo.style.color = '#fff';
        btnYt.style.background = 'transparent';
        btnYt.style.borderColor = 'var(--border)';
        btnYt.style.color = 'var(--text-muted)';
      } else {
        btnYt.style.background = '#1e293b';
        btnYt.style.borderColor = '#ef4444';
        btnYt.style.color = '#fff';
        btnSpo.style.background = 'transparent';
        btnSpo.style.borderColor = 'var(--border)';
        btnSpo.style.color = 'var(--text-muted)';
      }

      if (doSearch && document.getElementById('replaceSearchInput').value.trim()) {
        searchReplaceVersions();
      }
    }

    async function searchReplaceVersions() {
      const q = document.getElementById('replaceSearchInput').value.trim();
      const resultsEl = document.getElementById('replaceSearchResults');
      const statusEl = document.getElementById('replaceSearchStatus');
      if (!q) return;

      resultsEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: var(--text-muted); font-size: 0.85rem;">⏳ Wyszukiwanie wersji (${replaceSearchMode === 'spotify' ? 'Spotify' : 'YouTube'})...</div>`;
      statusEl.style.display = 'none';

      try {
        if (replaceSearchMode === 'spotify') {
          const res = await fetch(`${API_BASE}/search?q=${encodeURIComponent(q)}&type=track&limit=6`);
          const data = await res.json();
          if (!res.ok || !data.results || data.results.length === 0) {
            resultsEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: #f59e0b; font-size: 0.85rem;">⚠️ Nie znaleziono pasujących utworów w katalogu Spotify. Spróbuj zmienić zapytanie lub przełącz na zakładkę YouTube.</div>`;
            return;
          }

          resultsEl.innerHTML = data.results.map(item => {
            const durStr = formatDuration(item.duration_ms);
            const inLib = Boolean(item.in_library);
            const targetUrl = item.spotify_url || `${item.artist} - ${item.title}`;
            const encTarget = encodeURIComponent(targetUrl);
            const prevQ = encodeURIComponent(`${item.artist} - ${item.title}`);

            return `
              <div style="display: flex; align-items: center; justify-content: space-between; gap: 0.8rem; padding: 0.7rem; background: #0f172a; border-radius: 8px; border: 1px solid var(--border);">
                <div style="display: flex; align-items: center; gap: 0.7rem; min-width: 0; flex: 1;">
                  <img src="${item.cover_url || 'https://via.placeholder.com/48'}" alt="cover" style="width: 48px; height: 48px; border-radius: 6px; object-fit: cover; background: #1e293b; border: 1px solid var(--border); flex-shrink: 0;">
                  <div style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;">
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                      <span style="font-weight: 600; font-size: 0.9rem; color: #fff;">${escapeHtml(item.title)}</span>
                      ${durStr ? `<span class="badge-duration" style="font-size: 0.72rem; padding: 2px 6px;">⏱️ ${durStr}</span>` : ''}
                      ${inLib ? `<span class="badge-in-library" style="font-size: 0.72rem; padding: 2px 6px;">⚠️ Obecny w bazie</span>` : ''}
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis;">
                      ${escapeHtml(item.artist)} • <b style="color: #cbd5e1;">${escapeHtml(item.album)}</b> (${escapeHtml(item.year || '')})
                    </div>
                  </div>
                </div>
                <div style="display: flex; gap: 0.4rem; align-items: center; flex: 0 0 auto;">
                  <button class="btn-secondary" onclick="playOnlinePreview(decodeURIComponent('${prevQ}'), decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.artist)}'))" style="padding: 0.4rem 0.7rem; font-size: 0.8rem; white-space: nowrap;" title="Odsłuchaj fragment audio">
                    ▶️ Odsłuchaj
                  </button>
                  <button class="btn-primary" onclick="confirmReplaceTrack('${encTarget}', decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.artist)}'), decodeURIComponent('${encodeURIComponent(item.album)}'))" style="padding: 0.4rem 0.85rem; font-size: 0.8rem; white-space: nowrap; width: auto;">
                    🔄 Podmień
                  </button>
                </div>
              </div>
            `;
          }).join('');

        } else {
          // YouTube
          const res = await fetch(`${API_BASE}/search/youtube?q=${encodeURIComponent(q)}&limit=6`);
          const data = await res.json();
          if (!res.ok || !data.results || data.results.length === 0) {
            resultsEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: #f59e0b; font-size: 0.85rem;">⚠️ Nie znaleziono pasujących filmów/nagrań na YouTube.</div>`;
            return;
          }

          resultsEl.innerHTML = data.results.map(item => {
            const durStr = formatDuration(item.duration_seconds ? item.duration_seconds * 1000 : null);
            const targetUrl = item.webpage_url || item.url;
            const encTarget = encodeURIComponent(targetUrl);

            return `
              <div style="display: flex; align-items: center; justify-content: space-between; gap: 0.8rem; padding: 0.7rem; background: #0f172a; border-radius: 8px; border: 1px solid var(--border);">
                <div style="display: flex; align-items: center; gap: 0.7rem; min-width: 0; flex: 1;">
                  <img src="${item.thumbnail || 'https://via.placeholder.com/48'}" alt="thumb" style="width: 56px; height: 42px; border-radius: 6px; object-fit: cover; background: #1e293b; border: 1px solid var(--border); flex-shrink: 0;">
                  <div style="min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; flex: 1;">
                    <div style="display: flex; align-items: center; gap: 0.5rem; flex-wrap: wrap;">
                      <span style="font-weight: 600; font-size: 0.88rem; color: #fff;">${escapeHtml(item.title)}</span>
                      ${durStr ? `<span class="badge-duration" style="font-size: 0.72rem; padding: 2px 6px;">⏱️ ${durStr}</span>` : ''}
                    </div>
                    <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 2px; overflow: hidden; text-overflow: ellipsis;">
                      Kanał: <b style="color: #cbd5e1;">${escapeHtml(item.uploader || 'YouTube')}</b>
                    </div>
                  </div>
                </div>
                <div style="display: flex; gap: 0.4rem; align-items: center; flex: 0 0 auto;">
                  <button class="btn-secondary" onclick="playOnlinePreview(decodeURIComponent('${encTarget}'), decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.uploader || '')}'))" style="padding: 0.4rem 0.7rem; font-size: 0.8rem; white-space: nowrap;" title="Odsłuchaj fragment audio">
                    ▶️ Odsłuchaj
                  </button>
                  <button class="btn-primary" onclick="confirmReplaceTrack('${encTarget}', decodeURIComponent('${encodeURIComponent(item.title)}'), decodeURIComponent('${encodeURIComponent(item.uploader || '')}'), '')" style="padding: 0.4rem 0.85rem; font-size: 0.8rem; white-space: nowrap; width: auto;">
                    🔄 Podmień
                  </button>
                </div>
              </div>
            `;
          }).join('');
        }
      } catch (err) {
        resultsEl.innerHTML = `<div style="text-align: center; padding: 2rem; color: #ef4444; font-size: 0.85rem;">Błąd wyszukiwania: ${err}</div>`;
      }
    }

    async function confirmReplaceTrack(encUrl, title, artist, album) {
      if (!currentReplaceTrack) return;
      const targetUrl = decodeURIComponent(encUrl);
      const conf = confirm(`Czy na pewno chcesz podmienić utwór:\n"${currentReplaceTrack.title || currentReplaceTrack.filename}"\n\nna wersję:\n"${title}" (${artist})?\n\nDotychczasowy plik zostanie usunięty i zastąpiony nowym audio.`);
      if (!conf) return;

      const statusEl = document.getElementById('replaceSearchStatus');
      statusEl.className = 'status-box';
      statusEl.style.display = 'block';
      statusEl.innerHTML = `⏳ Rozpoczynanie pobierania nowej wersji i podmiany pliku... To może potrwać kilkanaście sekund.`;

      try {
        const res = await fetch(`${API_BASE}/library/replace-track`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            old_rel_path: currentReplaceTrack.rel_path,
            new_query_or_url: targetUrl,
            audio_format: currentReplaceTrack.format ? currentReplaceTrack.format.toLowerCase() : 'opus',
            custom_title: title || undefined,
            custom_artist: artist || undefined,
            custom_album: album || undefined
          })
        });
        const data = await res.json();
        if (res.ok) {
          showToast(`✅ Pomyślnie podmieniono utwór na: <b>${escapeHtml(title)}</b>`);
          closeReplaceTrackModal();
          loadLibraryTracks();
        } else {
          statusEl.className = 'status-box error';
          statusEl.innerHTML = `❌ Błąd podmiany: ${data.detail || 'Nieznany błąd'}`;
        }
      } catch (err) {
        statusEl.className = 'status-box error';
        statusEl.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }
  </script>

  <!-- DOLNY MINI-ODTWARZACZ AUDIO -->
  <div id="audioPlayerBar" class="audio-player-bar" style="display: none;">
    <div style="display: flex; align-items: center; gap: 1rem; width: 100%; max-width: 900px; margin: 0 auto; flex-wrap: wrap;">
      <div style="flex: 0 0 auto; display: flex; align-items: center; justify-content: center; width: 44px; height: 44px; background: rgba(29, 185, 84, 0.2); border-radius: 50%; color: var(--primary); font-size: 1.3rem;">
        🎵
      </div>
      <div style="min-width: 140px; flex: 1;">
        <div id="playerTrackTitle" style="font-weight: 700; font-size: 0.95rem; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #fff;">Tytuł utworu</div>
        <div id="playerTrackArtist" style="font-size: 0.8rem; color: var(--text-muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis;">Wykonawca • Album</div>
      </div>
      <div style="flex: 2; min-width: 200px; display: flex; align-items: center;">
        <audio id="globalAudioPlayer" controls style="width: 100%; height: 36px; outline: none;"></audio>
      </div>
      <div style="flex: 0 0 auto;">
        <button onclick="closeAudioPlayer()" style="background: transparent; border: none; color: var(--text-muted); font-size: 1.2rem; cursor: pointer; padding: 4px 8px; border-radius: 4px;" title="Zamknij odtwarzacz">✕</button>
      </div>
    </div>
  </div>

  <!-- MODAL: DODAJ UTWÓR DO PLAYLISTY -->
  <div id="addToPlaylistModal" class="modal-overlay" style="display: none;">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem;">
        <h3 style="font-size: 1.1rem; font-weight: 700; color: #fff;">➕ Dodaj utwór do playlisty</h3>
        <button onclick="closeAddToPlaylistModal()" style="background: transparent; border: none; color: var(--text-muted); font-size: 1.2rem; cursor: pointer;">✕</button>
      </div>
      <div style="margin-bottom: 1rem;">
        <div style="font-size: 0.85rem; color: var(--text-muted);">Wybrany utwór:</div>
        <div id="modalTrackTitle" style="font-weight: 700; font-size: 1rem; color: #fff; margin-top: 0.2rem;"></div>
        <div id="modalTrackArtist" style="font-size: 0.85rem; color: var(--primary);"></div>
      </div>
      <div class="form-group" style="margin-bottom: 1.4rem;">
        <label for="modalPlaylistSelect">Wybierz docelową playlistę w Navidrome:</label>
        <select id="modalPlaylistSelect" style="width: 100%;">
          <option value="" disabled selected>Ładowanie playlist...</option>
        </select>
      </div>
      <div style="display: flex; justify-content: flex-end; gap: 0.8rem;">
        <button class="btn-secondary" onclick="closeAddToPlaylistModal()">Anuluj</button>
        <button class="btn-primary" id="btnSubmitAddToPlaylist" onclick="submitAddToPlaylist()">➕ Dodaj</button>
      </div>
    </div>
  </div>

  <!-- MODAL: EDYCJA METADANYCH UTWORU -->
  <div id="editMetadataModal" class="modal-overlay" style="display: none;">
    <div class="modal-box">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem;">
        <h3 style="font-size: 1.15rem; font-weight: 700; color: #fff;">✏️ Edycja metadanych utworu</h3>
        <button onclick="closeEditMetadataModal()" style="background: transparent; border: none; color: var(--text-muted); font-size: 1.2rem; cursor: pointer;">✕</button>
      </div>

      <div style="display: flex; gap: 1rem; align-items: center; margin-bottom: 1.2rem; background: #0f172a; padding: 0.8rem; border-radius: 8px; border: 1px solid var(--border);">
        <img id="editModalCover" src="" style="width: 56px; height: 56px; border-radius: 6px; object-fit: cover; background: #1e293b; border: 1px solid var(--border);">
        <div style="min-width: 0; flex: 1;">
          <div id="editModalOriginalPath" style="font-size: 0.75rem; color: #64748b; font-family: monospace; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;"></div>
          <div style="font-size: 0.8rem; color: var(--text-muted); margin-top: 0.2rem;">Zmień tytuł, wykonawcę (np. latynizacja japońskich nazw) oraz album. Zmiany zostaną zapisane w tagach pliku audio.</div>
        </div>
      </div>

      <div class="form-group" style="margin-bottom: 1rem;">
        <label for="editTrackTitle">Tytuł utworu:</label>
        <input type="text" id="editTrackTitle" placeholder="np. Tytuł zromanizowany lub przetłumaczony" style="width: 100%;">
      </div>

      <div class="form-group" style="margin-bottom: 1rem;">
        <label for="editTrackArtist">Wykonawca (wpisz lub wybierz z listy):</label>
        <input type="text" id="editTrackArtist" list="libraryArtistsDatalist" placeholder="np. Hanabie, Ado..." style="width: 100%;">
      </div>

      <div class="form-group" style="margin-bottom: 1.4rem;">
        <label for="editTrackAlbum">Album (wpisz lub wybierz z listy):</label>
        <input type="text" id="editTrackAlbum" list="libraryAlbumsDatalist" placeholder="np. Nazwa albumu..." style="width: 100%;">
      </div>

      <div style="display: flex; justify-content: flex-end; gap: 0.8rem;">
        <button class="btn-secondary" onclick="closeEditMetadataModal()">Anuluj</button>
        <button class="btn-primary" id="btnSaveMetadata" onclick="submitSaveMetadata()">💾 Zapisz zmiany</button>
      </div>
    </div>
  </div>

  <!-- MODAL: PODMIANA UTWORU -->
  <div id="replaceTrackModal" class="modal-overlay" style="display: none;">
    <div class="modal-box" style="max-width: 680px; max-height: 90vh; overflow-y: auto;">
      <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 1.2rem; border-bottom: 1px solid var(--border); padding-bottom: 0.8rem;">
        <h3 style="font-size: 1.15rem; font-weight: 700; color: #fff;">🔄 Podmień utwór na inną wersję</h3>
        <button onclick="closeReplaceTrackModal()" style="background: transparent; border: none; color: var(--text-muted); font-size: 1.2rem; cursor: pointer;">✕</button>
      </div>

      <!-- Aktualny utwór -->
      <div style="display: flex; gap: 1rem; align-items: center; margin-bottom: 1.2rem; background: #0f172a; padding: 0.8rem; border-radius: 8px; border: 1px solid var(--border);">
        <img id="replaceModalCurrentCover" src="" style="width: 58px; height: 58px; border-radius: 6px; object-fit: cover; background: #1e293b; border: 1px solid var(--border);">
        <div style="min-width: 0; flex: 1;">
          <div style="font-size: 0.72rem; text-transform: uppercase; color: #ef4444; font-weight: 700;">Aktualnie w bibliotece (do zastąpienia):</div>
          <div id="replaceModalCurrentTitle" style="font-weight: 700; font-size: 0.95rem; color: #fff; margin-top: 0.1rem;"></div>
          <div id="replaceModalCurrentArtist" style="font-size: 0.82rem; color: var(--text-muted);"></div>
          <div id="replaceModalCurrentMeta" style="font-size: 0.75rem; color: #64748b; margin-top: 0.15rem;"></div>
        </div>
      </div>

      <!-- Wyszukiwarka nowej wersji -->
      <div class="form-group" style="margin-bottom: 0.8rem;">
        <label for="replaceSearchInput">Szukaj innej wersji utworu (Spotify / YouTube):</label>
        <div style="display: flex; gap: 0.6rem;">
          <input type="text" id="replaceSearchInput" placeholder="Wpisz nazwę lub wklej link ze Spotify / YouTube" onkeydown="if(event.key==='Enter') searchReplaceVersions()">
          <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0 1.2rem;" onclick="searchReplaceVersions()">🔍 Szukaj</button>
        </div>
      </div>

      <div style="display: flex; gap: 0.6rem; margin-bottom: 1rem;">
        <button class="btn-secondary" id="btnReplaceTabSpotify" onclick="switchReplaceSearchMode('spotify')" style="padding: 0.4rem 0.85rem; font-size: 0.82rem; background: #1e293b; border-color: var(--primary);">🟢 Spotify (Wydania studyjne)</button>
        <button class="btn-secondary" id="btnReplaceTabYouTube" onclick="switchReplaceSearchMode('youtube')" style="padding: 0.4rem 0.85rem; font-size: 0.82rem;">🔴 YouTube (Konkretne wideo / audio)</button>
      </div>

      <div id="replaceSearchStatus" class="status-box" style="display: none; margin-bottom: 1rem;"></div>

      <!-- Lista znalezionych wersji -->
      <div id="replaceSearchResults" style="display: flex; flex-direction: column; gap: 0.6rem; max-height: 380px; overflow-y: auto; padding-right: 4px;">
        <div style="text-align: center; padding: 2rem; color: var(--text-muted); font-size: 0.85rem;">
          Wpisz tytuł lub wykonawcę i kliknij „🔍 Szukaj”, aby znaleźć alternatywne wersje.
        </div>
      </div>

      <div style="display: flex; justify-content: flex-end; margin-top: 1.2rem; border-top: 1px solid var(--border); padding-top: 0.8rem;">
        <button class="btn-secondary" onclick="closeReplaceTrackModal()">Zamknij</button>
      </div>
    </div>
  </div>

  <!-- LISTY AUTOCOMPLETE DLA AUTORÓW I ALBUMÓW -->
  <datalist id="libraryArtistsDatalist"></datalist>
  <datalist id="libraryAlbumsDatalist"></datalist>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
