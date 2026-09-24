import logging
from typing import Optional, List
from fastapi import FastAPI, BackgroundTasks, HTTPException, Header, Depends, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field

from config import settings
from spotify_client import spotify_manager
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
    query_or_url: str = Field(..., description="Link Spotify (utwór/album/playlista) lub zapytanie tekstowe 'Artysta - Tytuł'")
    format: Optional[str] = Field(default="opus", description="Format audio: opus (rekomendowany), mp3, flac")
    force: bool = Field(default=False, description="Czy wymusić ponowne pobranie, jeśli plik już istnieje")


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
      max-width: 760px;
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
      padding: 1.8rem;
      margin-bottom: 1.5rem;
      box-shadow: 0 10px 25px -5px rgba(0, 0, 0, 0.3);
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
      <p>Prywatny ekosystem muzyczny • On-Demand Spotify to Opus Engine</p>
    </header>

    <div class="card">
      <div class="form-group">
        <label for="queryInput">Wklej link ze Spotify (utwór/album/playlista) lub wpisz frazę:</label>
        <div style="display: flex; gap: 0.6rem;">
          <input type="text" id="queryInput" placeholder="np. https://open.spotify.com/playlist/... lub Dawid Podsiadło" onkeydown="if(event.key==='Enter') searchMusic()">
          <button class="btn-primary" style="flex: 0 0 auto; width: auto; padding: 0 1.4rem;" onclick="searchMusic()">🔍 Szukaj</button>
        </div>
      </div>
      
      <div style="display: flex; gap: 1rem; margin-bottom: 0.8rem; flex-wrap: wrap;">
        <div style="flex: 1; min-width: 160px;">
          <label for="searchTypeSelect">Typ wyszukiwania:</label>
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

      <div class="btn-row">
        <button class="btn-primary" onclick="startDownload()">⬇️ Pobierz wklejony link do biblioteki</button>
        <button class="btn-secondary" onclick="refreshNavidrome()">🔄 Odśwież Navidrome</button>
      </div>

      <div id="statusBox" class="status-box"></div>
      <div id="searchResults" class="results-grid"></div>
    </div>

    <div class="nav-links">
      <a href="http://" + window.location.hostname + ":4533" target="_blank" id="navidromeLink">Otwórz Navidrome Web UI ↗</a>
      <a href="/docs" target="_blank">Dokumentacja Swagger API ↗</a>
    </div>
  </div>

  <script>
    document.getElementById("navidromeLink").href = "http://" + window.location.hostname + ":4533";

    async function startDownload(customQuery = null) {
      const q = customQuery || document.getElementById('queryInput').value.trim();
      const format = document.getElementById('formatSelect').value;
      const force = document.getElementById('forceCheck').checked;
      const statusBox = document.getElementById('statusBox');

      if (!q) {
        alert("Wpisz zapytanie lub wklej link Spotify!");
        return;
      }

      statusBox.style.display = "block";
      statusBox.innerHTML = "⏳ Kolejkowanie zadania...";

      try {
        const res = await fetch('/download', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ query_or_url: q, format: format, force: force })
        });
        const data = await res.json();
        if (res.ok) {
          statusBox.innerHTML = `🚀 <b>Zadanie zlecone!</b> ID: <code>${data.task_id}</code>. Trwa pobieranie...`;
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

    async function searchMusic() {
      const q = document.getElementById('queryInput').value.trim();
      const type = document.getElementById('searchTypeSelect').value;
      const resultsContainer = document.getElementById('searchResults');
      if (!q) return;

      // Jeśli wklejono bezpośredni link do Spotify, od razu uruchom pobieranie
      if (q.includes('open.spotify.com/')) {
        startDownload(q);
        return;
      }

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
                <button class="download-small-btn" onclick="startDownload('${item.spotify_url}')">⬇️ Pobierz playlistę</button>
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

    async function refreshNavidrome() {
      const statusBox = document.getElementById('statusBox');
      statusBox.style.display = "block";
      statusBox.innerHTML = "Wysyłanie sygnału reskanu do Navidrome...";
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
