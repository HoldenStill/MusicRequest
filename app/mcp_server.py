"""Built-in Streamable HTTP MCP Server for MusicRequest.

Exposes Qobuz search, album tracklist lookup, download queueing, queue status,
and quick-download tools directly inside the MusicRequest FastAPI process.
"""

from __future__ import annotations

import hmac
import json
import re
from typing import Any, Callable

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

mcp_server = MCPServer("musicrequest-api")


def _get_handler() -> Any:
    """Lazily retrieve the active StreamripHandler from app.main."""
    from app import main as app_main

    if app_main.handler is None:
        raise ToolError(
            "MusicRequest handler is not initialized yet. Please wait for server startup."
        )
    return app_main.handler


def _compact_search_item(item: dict[str, Any]) -> dict[str, Any]:
    """Trim verbose API response fields down to essential LLM-friendly metadata."""
    item_type = item.get("type", "album")
    out: dict[str, Any] = {"type": item_type}

    if item_type == "album":
        out["album_id"] = item.get("album_id")
        out["title"] = item.get("title")
        out["artist"] = item.get("artist")
        if item.get("release_year"):
            out["year"] = item["release_year"]
        if item.get("tracks_count"):
            out["tracks"] = item["tracks_count"]
        if item.get("hires") is not None:
            out["hires"] = item["hires"]
        if item.get("maximum_bit_depth") and item.get("maximum_sampling_rate"):
            out["quality_spec"] = f"{item['maximum_bit_depth']}-bit/{item['maximum_sampling_rate']}kHz"
        if item.get("duration_seconds"):
            out["duration_sec"] = item["duration_seconds"]

    elif item_type == "track":
        out["track_id"] = item.get("track_id")
        out["title"] = item.get("title")
        out["artist"] = item.get("artist")
        out["album"] = item.get("album")
        out["album_id"] = item.get("album_id")
        if item.get("hires") is not None:
            out["hires"] = item["hires"]
        if item.get("duration_seconds"):
            out["duration_sec"] = item["duration_seconds"]
        if item.get("track_number"):
            out["track_no"] = item["track_number"]

    elif item_type == "artist":
        out["artist_id"] = item.get("artist_id")
        out["name"] = item.get("name")
        if item.get("albums_count"):
            out["albums_count"] = item["albums_count"]

    return {k: v for k, v in out.items() if v is not None}


def _compact_job(job: dict[str, Any]) -> dict[str, Any]:
    """Format download job for compact status output."""
    out: dict[str, Any] = {
        "job_id": job.get("job_id"),
        "title": job.get("title"),
        "artist": job.get("artist"),
        "status": job.get("status"),
        "download_type": job.get("download_type", "album"),
    }
    if job.get("album_id"):
        out["album_id"] = job["album_id"]
    if job.get("track_id"):
        out["track_id"] = job["track_id"]
    if job.get("quality"):
        out["quality"] = job["quality"]
    if job.get("error"):
        out["error"] = job["error"]
    if job.get("queued_at"):
        out["queued_at"] = job["queued_at"]
    return out


_NON_ALNUM_RE = re.compile(r"[^a-z0-9\s]+")


def _norm_text(text: str) -> str:
    """Normalize text for relevance scoring (lowercase, alphanumeric + collapsed spaces)."""
    return " ".join(_NON_ALNUM_RE.sub(" ", (text or "").lower()).split())


def _rank_and_filter_search_results(
    items: list[dict[str, Any]], query: str
) -> list[dict[str, Any]]:
    """Rank exact/strong title+artist matches first and trim obvious low-relevance noise."""
    norm_q = _norm_text(query)
    if not norm_q or not items:
        return items
    q_tokens = set(norm_q.split())

    scored: list[tuple[float, int, dict[str, Any]]] = []
    for idx, item in enumerate(items):
        norm_title = _norm_text(item.get("title") or item.get("name") or "")
        norm_artist = _norm_text(item.get("artist") or "")
        combined_tokens = set(f"{norm_title} {norm_artist}".split())

        score = 0.0
        if norm_title == norm_q or norm_artist == norm_q:
            score += 100.0
        elif norm_q in norm_title or norm_q in norm_artist:
            score += 50.0
        elif q_tokens and q_tokens.issubset(combined_tokens):
            score += 25.0

        if q_tokens:
            overlap = len(q_tokens & combined_tokens) / len(q_tokens)
            score += 10.0 * overlap

        scored.append((score, -idx, item))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)

    # If we have at least one strong match (exact, substring, or all tokens matched),
    # trim obvious noise where fewer than half the query tokens appear.
    has_strong_match = scored[0][0] >= 25.0
    if has_strong_match:
        filtered = [item for score, _, item in scored if score >= 5.0]
        if filtered:
            return filtered

    return [item for _, _, item in scored]


@mcp_server.tool()
async def musicrequest_search(
    query: str,
    type: str = "album",
    limit: int = 10,
) -> list[dict[str, Any]]:
    """Search Qobuz catalog via MusicRequest for albums, tracks, or artists.
    query: Title, artist name, or keywords.
    type: Search target type — 'album' (default), 'track', or 'artist'.
    limit: Maximum results to return (1-20, default 10).
    """
    handler = _get_handler()
    safe_type = type.lower().strip()
    if safe_type not in ("album", "track", "artist"):
        safe_type = "album"

    safe_limit = min(max(int(limit), 1), 20)
    data = await handler.search(query, safe_limit, media_type=safe_type)
    if not isinstance(data, list):
        return []
    ranked = _rank_and_filter_search_results(data, query)
    return [_compact_search_item(i) for i in ranked]


@mcp_server.tool()
async def musicrequest_get_album_tracks(album_id: str) -> dict[str, Any]:
    """Get album tracklist and metadata from MusicRequest for a specific Qobuz album ID.
    album_id: Qobuz album identifier (e.g. '0060253738012' or 'yauvpj1r6g2ha').
    """
    handler = _get_handler()
    clean_id = album_id.strip()
    data = await handler.get_album_tracks(clean_id)
    if not data:
        raise ToolError(f"Album '{clean_id}' not found.")

    tracks = [
        {
            "track_id": t.get("track_id"),
            "track_no": t.get("track_number"),
            "title": t.get("title"),
            "duration_sec": t.get("duration") if t.get("duration") is not None else t.get("duration_seconds"),
            "hires": bool(t.get("hires") or ((t.get("bit_depth") or 16) > 16)),
        }
        for t in data.get("tracks", [])
    ]

    return {
        "album_id": data.get("album_id", clean_id),
        "title": data.get("title"),
        "artist": data.get("artist"),
        "release_year": data.get("release_year"),
        "total_tracks": len(tracks),
        "tracks": tracks,
    }


@mcp_server.tool()
async def musicrequest_download(
    album_id: str,
    title: str,
    artist: str,
    download_type: str = "album",
    track_id: str | None = None,
    quality: int | None = None,
    cover_art_url: str = "",
) -> dict[str, Any]:
    """Enqueue an album or single track to be downloaded by MusicRequest.
    album_id: Qobuz album identifier.
    title: Album or track title.
    artist: Artist name.
    download_type: 'album' (default) or 'track'.
    track_id: Required if download_type is 'track'.
    quality: Audio quality override (1: 320k MP3, 2: 16/44.1 CD FLAC, 3: 24/<=96 Hi-Res, 4: 24/>=96 Hi-Res).
             If omitted, uses the server default.
    cover_art_url: Optional cover art URL for display in queue.
    """
    handler = _get_handler()
    safe_type = download_type.lower().strip()
    if safe_type not in ("album", "track"):
        safe_type = "album"

    job = await handler.enqueue_download(
        album_id=album_id.strip(),
        title=title.strip(),
        artist=artist.strip(),
        download_type=safe_type,
        track_id=track_id.strip() if track_id else None,
        quality=quality,
        cover_art_url=cover_art_url.strip(),
    )

    return {
        "status": "queued",
        "job_id": job.job_id,
        "title": title.strip(),
        "artist": artist.strip(),
        "download_type": safe_type,
    }


@mcp_server.tool()
async def musicrequest_queue_status(active_only: bool = False) -> dict[str, Any]:
    """Check current download queue status on MusicRequest.
    active_only: When True, returns active download and queue count without completed list.
    """
    handler = _get_handler()
    data = handler.get_queue_status()

    # StreamripHandler.get_queue_status() returns {"active": ..., "queued": ..., "completed": ...}
    raw_active = data.get("active") if "active" in data else data.get("active_job")
    raw_queued = data.get("queued") if "queued" in data else data.get("queued_jobs", [])
    raw_completed = data.get("completed") if "completed" in data else data.get("completed_jobs", [])

    active = _compact_job(raw_active) if raw_active else None
    queued = [_compact_job(j) for j in (raw_queued or [])]
    completed = [_compact_job(j) for j in (raw_completed or [])]

    if active_only:
        return {
            "active_job": active,
            "queued_count": len(queued),
            "completed_count": len(completed),
        }

    return {
        "active_job": active,
        "queued_jobs": queued,
        "completed_jobs": list(reversed(completed))[:10],
    }


@mcp_server.tool()
async def musicrequest_quick_download(
    query: str,
    artist: str = "",
    download_type: str = "album",
    quality: int | None = None,
) -> dict[str, Any]:
    """Search Qobuz and immediately enqueue the best match for download in a single step.
    query: Title of album or track to search for.
    artist: Optional artist name to filter or disambiguate search results.
    download_type: 'album' (default, downloads full album) or 'track' (downloads single song).
    quality: Quality override (1: 320k MP3, 2: 16/44.1 CD, 3: 24/<=96 Hi-Res, 4: 24/>=96 Hi-Res).
    """
    handler = _get_handler()
    safe_type = download_type.lower().strip()
    if safe_type not in ("album", "track"):
        safe_type = "album"

    search_results = await handler.search(query, 10, media_type=safe_type)
    if not search_results or not isinstance(search_results, list):
        return {
            "status": "error",
            "message": f"No {safe_type} matches found on Qobuz for query '{query}'.",
        }
    search_results = _rank_and_filter_search_results(search_results, query)

    best_match: dict[str, Any] | None = None
    if artist.strip():
        target_artist = artist.strip().lower()
        for item in search_results:
            item_artist = (item.get("artist") or "").strip().lower()
            if target_artist in item_artist or item_artist in target_artist:
                best_match = item
                break

    if not best_match:
        best_match = search_results[0]

    album_id = best_match.get("album_id") or ""
    title = best_match.get("title") or query
    matched_artist = best_match.get("artist") or artist or "Unknown"
    track_id = best_match.get("track_id") if safe_type == "track" else None
    cover_art_url = best_match.get("cover_art_url") or ""

    if not album_id and not track_id:
        return {
            "status": "error",
            "message": "Candidate found but missing required album/track ID.",
            "candidate": best_match,
        }

    job = await handler.enqueue_download(
        album_id=album_id,
        title=title,
        artist=matched_artist,
        download_type=safe_type,
        track_id=track_id,
        quality=quality,
        cover_art_url=cover_art_url,
    )

    return {
        "status": "queued",
        "job_id": job.job_id,
        "download_type": safe_type,
        "quality": quality,
        "matched_item": {
            "title": title,
            "artist": matched_artist,
            "album_id": album_id,
            "track_id": track_id,
            "hires": best_match.get("hires", False),
            "year": best_match.get("release_year"),
        },
    }


class BearerAuthASGIApp:
    """ASGI wrapper enforcing Authorization: Bearer <token> using constant-time comparison."""

    def __init__(self, app: Any, token_getter: Callable[[], str]) -> None:
        self.app = app
        self.token_getter = token_getter

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        expected = self.token_getter()
        if not expected:
            await self._send_unauthorized(send)
            return

        auth_header = ""
        for name, value in scope.get("headers", []):
            if name.lower() == b"authorization":
                auth_header = value.decode("latin-1")
                break

        scheme, _, token = auth_header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            await self._send_unauthorized(send)
            return

        if not hmac.compare_digest(token.strip().encode("utf-8"), expected.encode("utf-8")):
            await self._send_unauthorized(send)
            return

        await self.app(scope, receive, send)

    @staticmethod
    async def _send_unauthorized(send: Any) -> None:
        body = json.dumps({"detail": "Unauthorized"}).encode("utf-8")
        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"www-authenticate", b"Bearer"),
            ],
        })
        await send({
            "type": "http.response.body",
            "body": body,
        })


def create_mcp_sub_app() -> Any:
    """Build a fresh Streamable HTTP Starlette sub-application (and session manager)."""
    # Disable localhost-only DNS rebinding protection so reverse proxies (Nginx Proxy Manager)
    # and LAN/Docker hostnames work seamlessly; /mcp is protected by Bearer token auth.
    return mcp_server.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )


def create_mcp_asgi_app(token_getter: Callable[[], str]) -> BearerAuthASGIApp:
    """Build the Streamable HTTP MCP sub-application wrapped with Bearer auth."""
    return BearerAuthASGIApp(create_mcp_sub_app(), token_getter)
