"""FastAPI application main entrypoint for MusicRequest."""

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import time
from pathlib import Path
from typing import Any, AsyncGenerator

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse

from app.streamrip_handler import StreamripHandler
from app.playlist_handler import PlaylistImporter

logger = logging.getLogger("musicrequest")

# ── Configuration ────────────────────────────────────────────────────────────
APP_PASSCODE = os.environ.get("APP_PASSCODE", "1099")
APP_SECRET_KEY = secrets.token_hex(32).encode("utf-8")
def _get_default_config_path() -> str:
    if "STREAMRIP_CONFIG_PATH" in os.environ:
        return os.environ["STREAMRIP_CONFIG_PATH"]
    if os.path.exists("/config/config.toml"):
        return "/config/config.toml"
    for candidate in [
        os.path.expanduser("~/AppData/Roaming/streamrip/config.toml"),
        os.path.expanduser("~/.config/streamrip/config.toml"),
    ]:
        if os.path.exists(candidate):
            return candidate
    return "/config/config.toml"


def _get_default_music_dir() -> str:
    if "MUSIC_DIR" in os.environ:
        return os.environ["MUSIC_DIR"]
    if os.path.exists("/music"):
        return "/music"
    local_music = os.path.expanduser("~/Music")
    if os.path.exists(local_music):
        return local_music
    return "/music"


STREAMRIP_CONFIG_PATH = _get_default_config_path()
MUSIC_DIR = _get_default_music_dir()
SEARCH_LIMIT = int(os.environ.get("SEARCH_LIMIT", "10"))
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
DEFAULT_QUALITY = int(os.environ.get("DEFAULT_QUALITY", os.environ.get("QOBUZ_QUALITY", "3")))

# Resolve paths relative to this file so it works in Docker AND local dev
_BASE_DIR = Path(__file__).resolve().parent
_STATIC_DIR = _BASE_DIR / "static"
_TEMPLATE_DIR = _BASE_DIR / "templates"

handler: StreamripHandler | None = None
playlist_importer: PlaylistImporter | None = None


# ── Lifespan ─────────────────────────────────────────────────────────────────
@contextlib.asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Initialize the StreamripHandler, PlaylistImporter, and start the download worker."""
    global handler, playlist_importer
    logger.info("Starting MusicRequest (config=%s, music=%s)", STREAMRIP_CONFIG_PATH, MUSIC_DIR)
    handler = StreamripHandler(config_path=STREAMRIP_CONFIG_PATH, music_dir=MUSIC_DIR)
    await handler.start_worker()
    playlist_importer = PlaylistImporter(
        music_dir=MUSIC_DIR,
        config_path=STREAMRIP_CONFIG_PATH,
        handler=handler,
        jellyfin_url=JELLYFIN_URL,
        jellyfin_api_key=JELLYFIN_API_KEY,
    )
    yield
    if handler:
        await handler.stop_worker()
    logger.info("MusicRequest shut down.")


# ── App creation ─────────────────────────────────────────────────────────────
app = FastAPI(title="MusicRequest", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(_TEMPLATE_DIR))


# ── Auth helpers ─────────────────────────────────────────────────────────────
def _generate_token() -> str:
    """Create an HMAC-SHA256 signed session token containing a timestamp."""
    timestamp = str(int(time.time()))
    signature = hmac.new(APP_SECRET_KEY, timestamp.encode(), "sha256").hexdigest()
    return f"{timestamp}.{signature}"


def _verify_token(token: str) -> bool:
    """Verify the signed session token is valid and not expired (30-day TTL)."""
    if not token or "." not in token:
        return False
    timestamp, signature = token.split(".", 1)
    try:
        if time.time() - int(timestamp) > 30 * 24 * 3600:
            return False
    except ValueError:
        return False
    expected = hmac.new(APP_SECRET_KEY, timestamp.encode(), "sha256").hexdigest()
    return hmac.compare_digest(signature, expected)


def _is_authenticated(request: Request) -> bool:
    """Check the session_token cookie on the request."""
    token = request.cookies.get("session_token")
    return bool(token and _verify_token(token))


async def require_auth(request: Request) -> None:
    """FastAPI dependency — raises 401 if the session cookie is missing/invalid."""
    if not _is_authenticated(request):
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Pydantic models ─────────────────────────────────────────────────────────
class AuthRequest(BaseModel):
    passcode: str


class DownloadRequest(BaseModel):
    album_id: str = ""
    title: str
    artist: str
    download_type: str = "album"       # "album" or "track"
    track_id: str | None = None
    quality: int | None = None
    cover_art_url: str = ""


class PlaylistAnalyzeRequest(BaseModel):
    url: str


class PlaylistImportRequest(BaseModel):
    url: str
    selected_indices: list[int] | None = None
    quality: int = 3


class SettingsRequest(BaseModel):
    jellyfin_url: str = ""
    jellyfin_api_key: str = ""
    default_quality: int = 3


# ── Routes ───────────────────────────────────────────────────────────────────
@app.post("/api/auth")
async def authenticate(body: AuthRequest, response: Response) -> dict[str, Any]:
    """Validate the passcode and set a signed HttpOnly session cookie."""
    if body.passcode != APP_PASSCODE:
        raise HTTPException(status_code=401, detail="Invalid passcode")
    response.set_cookie(
        key="session_token",
        value=_generate_token(),
        httponly=True,
        samesite="strict",
        max_age=30 * 24 * 3600,
    )
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response) -> dict[str, Any]:
    """Clear the session cookie."""
    response.delete_cookie("session_token")
    return {"ok": True}


@app.get("/", response_class=HTMLResponse)
async def root(request: Request) -> Response:
    """Serve the main app if authenticated, otherwise the lock screen."""
    template = "index.html" if _is_authenticated(request) else "login.html"
    return templates.TemplateResponse(template, {"request": request})


@app.get("/api/search", dependencies=[Depends(require_auth)])
async def search(q: str, type: str = "album", limit: int = SEARCH_LIMIT) -> list[dict[str, Any]]:
    """Search Qobuz for albums, tracks, or artists matching the query string."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")
    if type not in ("album", "track", "artist"):
        raise HTTPException(status_code=400, detail="Invalid search type. Use 'album', 'track', or 'artist'.")
    return await handler.search(q, limit, media_type=type)


@app.get("/api/album/{album_id}/tracks", dependencies=[Depends(require_auth)])
async def get_album_tracks(album_id: str) -> dict[str, Any]:
    """Fetch the full tracklist for a specific album."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")
    result = await handler.get_album_tracks(album_id)
    if not result:
        raise HTTPException(status_code=404, detail="Album not found")
    return result


@app.get("/api/artist/{artist_id}", dependencies=[Depends(require_auth)])
async def get_artist_details(artist_id: str) -> dict[str, Any]:
    """Fetch artist details including discography and top tracks."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")
    result = await handler.get_artist_details(artist_id)
    if not result:
        raise HTTPException(status_code=404, detail="Artist not found")
    return result


@app.post("/api/download", dependencies=[Depends(require_auth)])
async def enqueue_download(body: DownloadRequest) -> dict[str, Any]:
    """Add an album or track to the download queue."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")
    job = await handler.enqueue_download(
        album_id=body.album_id,
        title=body.title,
        artist=body.artist,
        download_type=body.download_type,
        track_id=body.track_id,
        quality=body.quality,
        cover_art_url=body.cover_art_url,
    )
    return {"job_id": job.job_id, "status": "queued"}


@app.get("/api/queue/status", dependencies=[Depends(require_auth)])
async def queue_status() -> dict[str, Any]:
    """Return a one-shot snapshot of the current queue state."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")
    return handler.get_queue_status()


@app.get("/api/queue", dependencies=[Depends(require_auth)])
async def queue_sse(request: Request) -> EventSourceResponse:
    """SSE stream of real-time queue status updates."""
    if not handler:
        raise HTTPException(status_code=503, detail="Handler not initialized")

    sub_queue = handler.subscribe()

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        try:
            # Send current state immediately on connect
            yield {"data": json.dumps(handler.get_queue_status())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(sub_queue.get(), timeout=15.0)
                    yield {"data": json.dumps(data)}
                except asyncio.TimeoutError:
                    # Keep-alive ping to prevent proxy/browser timeouts
                    yield {"event": "ping", "data": ""}
        finally:
            handler.unsubscribe(sub_queue)

    return EventSourceResponse(event_generator())


# ── Playlist Import Routes ───────────────────────────────────────────────────
# Cache analyzed playlists briefly so the import step doesn't re-scrape
_analyzed_playlists: dict[str, Any] = {}


@app.post("/api/playlist/analyze", dependencies=[Depends(require_auth)])
async def analyze_playlist(body: PlaylistAnalyzeRequest) -> dict[str, Any]:
    """Analyze a Spotify or Qobuz playlist URL: parse tracks, check duplicates, match on Qobuz."""
    if not playlist_importer:
        raise HTTPException(status_code=503, detail="Playlist importer not initialized")
    try:
        result = await playlist_importer.analyze_playlist(body.url)
        # Cache for the import step
        cache_key = f"{result.source}:{result.playlist_id}"
        _analyzed_playlists[cache_key] = result
        return result.to_dict()
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        logger.exception("Playlist analysis failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/playlist/import", dependencies=[Depends(require_auth)])
async def import_playlist(body: PlaylistImportRequest) -> dict[str, Any]:
    """Start importing missing tracks from an analyzed playlist."""
    if not playlist_importer:
        raise HTTPException(status_code=503, detail="Playlist importer not initialized")

    try:
        source, playlist_id = playlist_importer.parse_playlist_url(body.url)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    cache_key = f"{source}:{playlist_id}"
    playlist = _analyzed_playlists.get(cache_key)

    if not playlist:
        # Re-analyze if not cached
        try:
            playlist = await playlist_importer.analyze_playlist(body.url)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    try:
        job = await playlist_importer.import_playlist(
            playlist=playlist,
            selected_indices=body.selected_indices,
            quality=body.quality,
        )
        return job.to_dict()
    except Exception as e:
        logger.exception("Playlist import failed")
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/playlist/import/{import_id}/status", dependencies=[Depends(require_auth)])
async def import_status_sse(import_id: str, request: Request) -> EventSourceResponse:
    """SSE stream for playlist import progress updates."""
    if not playlist_importer:
        raise HTTPException(status_code=503, detail="Playlist importer not initialized")

    job = playlist_importer.import_jobs.get(import_id)
    if not job:
        raise HTTPException(status_code=404, detail="Import job not found")

    sub_queue = playlist_importer.subscribe_import(import_id)

    async def event_generator() -> AsyncGenerator[dict[str, Any], None]:
        try:
            # Send current state immediately
            yield {"data": json.dumps(job.to_dict())}
            while True:
                if await request.is_disconnected():
                    break
                try:
                    data = await asyncio.wait_for(sub_queue.get(), timeout=15.0)
                    yield {"data": json.dumps(data)}
                    if data.get("status") in ("complete", "failed"):
                        break
                except asyncio.TimeoutError:
                    yield {"event": "ping", "data": ""}
        finally:
            playlist_importer.unsubscribe_import(import_id, sub_queue)

    return EventSourceResponse(event_generator())


# ── Settings Routes ──────────────────────────────────────────────────────────
_SETTINGS_FILE = Path(os.environ.get("STREAMRIP_CONFIG_PATH", "/config/config.toml")).parent / "musicrequest_settings.json"


def _load_settings() -> dict[str, Any]:
    """Load persisted settings from disk, falling back to env vars."""
    settings: dict[str, Any] = {
        "jellyfin_url": JELLYFIN_URL,
        "jellyfin_api_key": JELLYFIN_API_KEY,
        "default_quality": DEFAULT_QUALITY,
    }
    if _SETTINGS_FILE.exists():
        try:
            saved = json.loads(_SETTINGS_FILE.read_text())
            settings.update(saved)
        except Exception:
            pass
    return settings


@app.get("/api/settings", dependencies=[Depends(require_auth)])
async def get_settings() -> dict[str, Any]:
    """Return current persisted settings (API key is masked for display)."""
    s = _load_settings()
    masked_key = ("*" * 8 + s["jellyfin_api_key"][-4:]) if len(s.get("jellyfin_api_key", "")) > 4 else ("*" * len(s.get("jellyfin_api_key", "")))
    return {
        "jellyfin_url": s.get("jellyfin_url", ""),
        "jellyfin_api_key_masked": masked_key,
        "jellyfin_configured": bool(s.get("jellyfin_url") and s.get("jellyfin_api_key")),
        "default_quality": s.get("default_quality", DEFAULT_QUALITY),
    }


@app.post("/api/settings", dependencies=[Depends(require_auth)])
async def save_settings(body: SettingsRequest) -> dict[str, Any]:
    """Persist settings and hot-reload the playlist importer."""
    settings = {
        "jellyfin_url": body.jellyfin_url.rstrip("/"),
        "jellyfin_api_key": body.jellyfin_api_key,
        "default_quality": body.default_quality,
    }
    try:
        _SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        _SETTINGS_FILE.write_text(json.dumps(settings, indent=2))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to save settings: {e}")

    # Hot-reload the live instance
    if playlist_importer:
        playlist_importer.jellyfin_url = settings["jellyfin_url"]
        playlist_importer.jellyfin_api_key = settings["jellyfin_api_key"]

    return {"ok": True}
