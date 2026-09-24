import os
import shutil
import logging
from typing import Optional, List, Dict, Any
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import settings
from spotify_client import spotify_manager, TrackMetadata, inspect_url
from downloader import downloader
from navidrome_client import navidrome_client
from tasks import task_manager, TaskInfo, TaskStatus

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
def verify_api_key(x_api_key: Optional[str] = Header(None)):
    if not settings.API_SECRET_KEY or settings.API_SECRET_KEY in ["", "change_this_secret_token_for_api_auth"]:
        return True
    if settings.API_SECRET_KEY != x_api_key:
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
    if not spotify_manager.is_configured:
        raise HTTPException(
            status_code=503,
            detail="Integracja ze Spotify nie jest skonfigurowana. Ustaw SPOTIFY_CLIENT_ID i SPOTIFY_CLIENT_SECRET w pliku .env"
        )
    try:
        results = spotify_manager.search(query=q, search_type=type, limit=limit)
        return {"query": q, "type": type, "count": len(results), "results": results}
    except Exception as e:
        logger.error(f"Błąd wyszukiwania: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/inspect", tags=["Downloader"])
def inspect_media_link(req: InspectRequest, _: bool = Depends(verify_api_key)):
    """
    Bada link ze Spotify (playlista, album, utwór) lub YouTube (playlista, film)
    i zwraca listę utworów z metadanymi do podglądu, selekcji i edycji.
    """
    try:
        data = inspect_url(req.url_or_query)
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


@app.get("/", response_class=HTMLResponse, tags=["UI"])
def web_dashboard():
    """
    Wbudowany, responsywny web UI do wyszukiwania, wklejania linków i pobierania on-demand.
    """
    html_content = """<!DOCTYPE html>
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
  </style>
</head>
<body>
  <div class="container">
    <header>
      <h1>🎵 Spoti-Downloader & Navidrome</h1>
      <p>Prywatny ekosystem muzyczny • On-Demand Spotify / YouTube to Opus Engine</p>
    </header>

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
        <button class="btn-secondary" onclick="startDownload()">⚡ Szybkie pobranie w tle (Automat)</button>
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
            ⬇️ Pobierz zaznaczone (<span id="selectedCount">0</span>)
          </button>
        </div>
      </div>

      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 0.8rem; margin-bottom: 1rem;">
        <div style="display: flex; gap: 0.6rem;">
          <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAll(true)">☑️ Zaznacz wszystkie</button>
          <button class="btn-secondary" style="padding: 0.45rem 0.85rem; font-size: 0.82rem;" onclick="toggleSelectAll(false)">⬜ Odznacz wszystkie</button>
        </div>
        <div style="color: var(--text-muted); font-size: 0.82rem;">
          💡 Zaznacz utwory do pobrania. Możesz edytować pola przed pobraniem!
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

    <div class="nav-links">
      <a href="http://" + window.location.hostname + ":4533" target="_blank" id="navidromeLink">Otwórz Navidrome Web UI ↗</a>
      <a href="/docs" target="_blank">Dokumentacja Swagger API ↗</a>
    </div>
  </div>

  <script>
    document.getElementById("navidromeLink").href = "http://" + window.location.hostname + ":4533";

    let currentInspectedTracks = [];
    let currentPlaylistName = "";

    function escapeHtml(text) {
      if (!text) return '';
      return String(text).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/'/g, '&#39;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
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
        const res = await fetch('/inspect', {
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
        div.innerHTML = `
          <input type="checkbox" id="check-${idx}" style="width: 20px; height: 20px; cursor: pointer;" checked onchange="updateSelectedCounter()">
          <img src="${t.cover_url || 'https://via.placeholder.com/50'}" style="width: 50px; height: 50px; border-radius: 6px; object-fit: cover; flex-shrink: 0;" alt="cover">
          <div class="track-inputs">
            <div>
              <span class="field-lbl">Tytuł:</span>
              <input type="text" id="title-${idx}" class="input-compact" value="${escapeHtml(t.title)}">
            </div>
            <div>
              <span class="field-lbl">Wykonawca:</span>
              <input type="text" id="artist-${idx}" class="input-compact" value="${escapeHtml(t.artist)}">
            </div>
            <div>
              <span class="field-lbl">Album:</span>
              <input type="text" id="album-${idx}" class="input-compact" value="${escapeHtml(t.album)}">
            </div>
            <div>
              <span class="field-lbl">Rok:</span>
              <input type="text" id="year-${idx}" class="input-compact" value="${escapeHtml(t.year || '2026')}">
            </div>
          </div>
          <button class="download-small-btn" onclick="downloadSingleCustomTrack(${idx})" title="Pobierz tylko ten utwór">⬇️ Pobierz</button>
        `;
        container.appendChild(div);
      });

      updateSelectedCounter();
    }

    function updateSelectedCounter() {
      let count = 0;
      currentInspectedTracks.forEach((_, idx) => {
        const cb = document.getElementById(`check-${idx}`);
        if (cb && cb.checked) count++;
      });
      document.getElementById('selectedCount').innerText = count;
    }

    function toggleSelectAll(checked) {
      currentInspectedTracks.forEach((_, idx) => {
        const cb = document.getElementById(`check-${idx}`);
        if (cb) cb.checked = checked;
      });
      updateSelectedCounter();
    }

    function getTrackMetadataFromRow(idx) {
      const original = currentInspectedTracks[idx];
      return {
        spotify_id: original.spotify_id || `item_${idx}`,
        title: document.getElementById(`title-${idx}`).value.trim() || original.title,
        artists: [document.getElementById(`artist-${idx}`).value.trim() || original.artist],
        artist: document.getElementById(`artist-${idx}`).value.trim() || original.artist,
        album: document.getElementById(`album-${idx}`).value.trim() || original.album,
        album_artist: document.getElementById(`artist-${idx}`).value.trim() || original.artist,
        track_number: original.track_number || (idx + 1),
        total_tracks: currentInspectedTracks.length,
        disc_number: 1,
        release_date: document.getElementById(`year-${idx}`).value.trim() || "2026",
        year: document.getElementById(`year-${idx}`).value.trim() || "2026",
        duration_ms: original.duration_ms || 0,
        cover_url: original.cover_url,
        isrc: original.isrc || null,
        spotify_url: original.spotify_url || "",
        youtube_url: original.youtube_url || null
      };
    }

    async function downloadSelectedTracks() {
      const selectedTracks = [];
      currentInspectedTracks.forEach((_, idx) => {
        const cb = document.getElementById(`check-${idx}`);
        if (cb && cb.checked) {
          selectedTracks.push(getTrackMetadataFromRow(idx));
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
      statusBox.innerHTML = `⏳ Kolejkowanie ${selectedTracks.length} wybranych utworów...`;
      statusBox.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch('/download-selected', {
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
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> Pobieranie ${selectedTracks.length} wybranych utworów...`;
          pollTask(data.task_id);
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    async function downloadSingleCustomTrack(idx) {
      const track = getTrackMetadataFromRow(idx);
      const format = document.getElementById('formatSelect').value;
      const force = document.getElementById('forceCheck').checked;
      const statusBox = document.getElementById('statusBox');

      statusBox.style.display = "block";
      statusBox.innerHTML = `⏳ Kolejkowanie utworu: <b>${track.artist} - ${track.title}</b>...`;
      statusBox.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch('/download-selected', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({
            tracks: [track],
            format: format,
            force: force
          })
        });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> Trwa pobieranie...`;
          pollTask(data.task_id);
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    // --- AUTOMATYCZNE POBRANIE W TLE ---
    async function startDownload(customQuery = null) {
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
      statusBox.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch('/download', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ query_or_url: q, format: format, force: force })
        });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> Trwa pobieranie...`;
          pollTask(data.task_id);
        } else {
          statusBox.innerHTML = `❌ Błąd: ${data.detail || data.message}`;
        }
      } catch (err) {
        statusBox.innerHTML = `❌ Błąd połączenia: ${err}`;
      }
    }

    async function pollTask(taskId) {
      const statusBox = document.getElementById('statusBox');
      const interval = setInterval(async () => {
        try {
          const res = await fetch(`/tasks/${taskId}`);
          const task = await res.json();
          if (task.status === 'processing') {
            statusBox.innerHTML = `⚙️ <b>Pobieranie w toku:</b> ${task.current_track || ''} (${task.completed_tracks}/${task.total_tracks})`;
          } else if (task.status === 'success') {
            clearInterval(interval);
            statusBox.innerHTML = `✅ <b>Gotowe!</b> Pobrano ${task.completed_tracks} utworów. Biblioteka Navidrome została natychmiast zaktualizowana!`;
          } else if (task.status === 'failed') {
            clearInterval(interval);
            statusBox.innerHTML = `❌ <b>Błąd zadania:</b> ${task.error_message || 'Nieznany błąd'}`;
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
        const res = await fetch(`/search?q=${encodeURIComponent(q)}&type=${type}&limit=8`);
        const data = await res.json();
        resultsContainer.innerHTML = "";

        if (data.results && data.results.length > 0) {
          data.results.forEach(item => {
            const div = document.createElement('div');
            div.className = 'track-item';
            
            if (type === 'track') {
              div.innerHTML = `
                <img src="${item.cover_url || 'https://via.placeholder.com/64'}" alt="cover">
                <div class="track-info">
                  <div class="track-title">${item.title}</div>
                  <div class="track-artist">${item.artist} • ${item.album} (${item.year})</div>
                </div>
                <button class="download-small-btn" onclick="startDownload('${item.spotify_url}')">⬇️ Pobierz</button>
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
      const confirmed = confirm("⚠️ UWAGA: Czy na pewno chcesz całkowicie usunąć WSZYSTKIE pliki muzyczne z biblioteki na serwerze?\n\nOperacja usunie wszystkie pobrane utwory, okładki i playlisty z dysku, a następnie zresetuje bazę Navidrome.\nTwoje konto i ustawienia Navidrome zostaną ZACHOWANE.");
      if (!confirmed) return;

      const statusBox = document.getElementById('statusBox');
      statusBox.style.display = "block";
      statusBox.innerHTML = "⏳ Czyszczenie biblioteki muzycznej na serwerze...";
      statusBox.scrollIntoView({ behavior: 'smooth' });

      try {
        const res = await fetch('/library/clear', { method: 'POST' });
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
        const res = await fetch('/refresh-navidrome', { method: 'POST' });
        const data = await res.json();
        statusBox.innerHTML = `🔄 Sygnał reskanu wysłany: ${data.status}`;
      } catch (err) {
        statusBox.innerHTML = `Błąd: ${err}`;
      }
    }
  </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)
