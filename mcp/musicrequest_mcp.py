# /// script
# requires-python = ">=3.11"
# dependencies = ["mcp>=1.0.0", "httpx>=0.27.0"]
# ///

"""MusicRequest MCP Server for Hermes Agent.

Provides structured tools to search Qobuz music, inspect album tracklists,
enqueue album or track downloads, check queue status, and perform quick
search-and-enqueue operations against a self-hosted MusicRequest instance.
"""

import os
from typing import Any
import httpx

try:
    from mcp.server.mcpserver import MCPServer
    mcp = MCPServer("musicrequest-api")
except ImportError:
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("musicrequest-api")

MUSICREQUEST_URL = os.environ.get("MUSICREQUEST_URL", "http://localhost:8080").rstrip("/")
MUSICREQUEST_PASSCODE = os.environ.get("MUSICREQUEST_PASSCODE", "1099")
_ENV_DEFAULT_QUALITY = os.environ.get("MUSICREQUEST_DEFAULT_QUALITY", "").strip()
MUSICREQUEST_DEFAULT_QUALITY: int | None = (
    int(_ENV_DEFAULT_QUALITY) if _ENV_DEFAULT_QUALITY.isdigit() else None
)

_cached_session_cookie: str | None = None


def _get_configured_quality(quality: int | None) -> int | None:
    """Return explicit quality override or environment default quality."""
    if quality is not None:
        return quality
    return MUSICREQUEST_DEFAULT_QUALITY


async def _get_auth_cookie(client: httpx.AsyncClient, force_refresh: bool = False) -> str:
    """Retrieve or renew session_token cookie via /api/auth."""
    global _cached_session_cookie
    if _cached_session_cookie and not force_refresh:
        return _cached_session_cookie

    auth_url = f"{MUSICREQUEST_URL}/api/auth"
    resp = await client.post(auth_url, json={"passcode": MUSICREQUEST_PASSCODE})
    if resp.status_code != 200:
        raise RuntimeError(
            f"Failed to authenticate with MusicRequest at {auth_url} (HTTP {resp.status_code}). "
            "Please check MUSICREQUEST_URL and MUSICREQUEST_PASSCODE."
        )

    # Extract session_token cookie
    token = resp.cookies.get("session_token")
    if not token:
        set_cookie = resp.headers.get("set-cookie", "")
        if "session_token=" in set_cookie:
            token = set_cookie.split("session_token=")[1].split(";")[0].strip()

    if not token:
        raise RuntimeError(
            "MusicRequest authentication succeeded but no session_token cookie was received."
        )

    _cached_session_cookie = token
    return _cached_session_cookie


async def _api_request(
    method: str,
    path: str,
    params: dict[str, Any] | None = None,
    json_data: dict[str, Any] | None = None,
) -> Any:
    """Execute authenticated API request to MusicRequest with automatic 401 token refresh."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        cookie = await _get_auth_cookie(client)
        headers = {
            "Cookie": f"session_token={cookie}",
            "Accept": "application/json",
        }

        resp = await client.request(
            method=method,
            url=f"{MUSICREQUEST_URL}{path}",
            params=params,
            json=json_data,
            headers=headers,
        )

        # Retry once on 401 Unauthorized in case session expired
        if resp.status_code == 401:
            cookie = await _get_auth_cookie(client, force_refresh=True)
            headers["Cookie"] = f"session_token={cookie}"
            resp = await client.request(
                method=method,
                url=f"{MUSICREQUEST_URL}{path}",
                params=params,
                json=json_data,
                headers=headers,
            )

        resp.raise_for_status()
        return resp.json()


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


@mcp.tool()
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
    safe_type = type.lower().strip()
    if safe_type not in ("album", "track", "artist"):
        safe_type = "album"

    safe_limit = min(max(int(limit), 1), 20)
    data = await _api_request(
        method="GET",
        path="/api/search",
        params={"q": query, "type": safe_type, "limit": safe_limit},
    )

    if not isinstance(data, list):
        return []

    return [_compact_search_item(i) for i in data]


@mcp.tool()
async def musicrequest_get_album_tracks(album_id: str) -> dict[str, Any]:
    """Get album tracklist and metadata from MusicRequest for a specific Qobuz album ID.
    album_id: Qobuz album identifier (e.g. '0060253738012' or 'yauvpj1r6g2ha').
    """
    clean_id = album_id.strip()
    data = await _api_request(
        method="GET",
        path=f"/api/album/{clean_id}/tracks",
    )

    tracks = []
    for t in data.get("tracks", []):
        tracks.append({
            "track_id": t.get("track_id"),
            "track_no": t.get("track_number"),
            "title": t.get("title"),
            "duration_sec": t.get("duration_seconds"),
            "hires": t.get("hires", False),
        })

    return {
        "album_id": data.get("album_id", clean_id),
        "title": data.get("title"),
        "artist": data.get("artist"),
        "release_year": data.get("release_year"),
        "total_tracks": len(tracks),
        "tracks": tracks,
    }


@mcp.tool()
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
             If omitted, uses the server or environment default.
    cover_art_url: Optional cover art URL for display in queue.
    """
    safe_type = download_type.lower().strip()
    if safe_type not in ("album", "track"):
        safe_type = "album"

    payload: dict[str, Any] = {
        "album_id": album_id.strip(),
        "title": title.strip(),
        "artist": artist.strip(),
        "download_type": safe_type,
        "track_id": track_id.strip() if track_id else None,
        "quality": _get_configured_quality(quality),
        "cover_art_url": cover_art_url.strip(),
    }

    result = await _api_request(
        method="POST",
        path="/api/download",
        json_data=payload,
    )

    return {
        "status": "queued",
        "job_id": result.get("job_id"),
        "title": title,
        "artist": artist,
        "download_type": safe_type,
    }


@mcp.tool()
async def musicrequest_queue_status(active_only: bool = False) -> dict[str, Any]:
    """Check current download queue status on MusicRequest.
    active_only: When True, returns active download and queue count without completed list.
    """
    data = await _api_request(
        method="GET",
        path="/api/queue/status",
    )

    active = _compact_job(data["active_job"]) if data.get("active_job") else None
    queued = [_compact_job(j) for j in data.get("queued_jobs", [])]

    if active_only:
        return {
            "active_job": active,
            "queued_count": len(queued),
            "completed_count": len(data.get("completed_jobs", [])),
        }

    completed = [_compact_job(j) for j in data.get("completed_jobs", [])]
    return {
        "active_job": active,
        "queued_jobs": queued,
        "completed_jobs": completed[:10],
    }


@mcp.tool()
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
    safe_type = download_type.lower().strip()
    if safe_type not in ("album", "track"):
        safe_type = "album"

    # Search for top candidates
    search_results = await _api_request(
        method="GET",
        path="/api/search",
        params={"q": query, "type": safe_type, "limit": 10},
    )

    if not search_results or not isinstance(search_results, list):
        return {
            "status": "error",
            "message": f"No {safe_type} matches found on Qobuz for query '{query}'.",
        }

    best_match: dict[str, Any] | None = None

    # If artist hint provided, look for artist match
    if artist.strip():
        target_artist = artist.strip().lower()
        for item in search_results:
            item_artist = (item.get("artist") or "").strip().lower()
            if target_artist in item_artist or item_artist in target_artist:
                best_match = item
                break

    # If no artist match found or no artist specified, choose top search result
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

    # Enqueue download
    chosen_quality = _get_configured_quality(quality)
    payload: dict[str, Any] = {
        "album_id": album_id,
        "title": title,
        "artist": matched_artist,
        "download_type": safe_type,
        "track_id": track_id,
        "quality": chosen_quality,
        "cover_art_url": cover_art_url,
    }

    dl_resp = await _api_request(
        method="POST",
        path="/api/download",
        json_data=payload,
    )

    return {
        "status": "queued",
        "job_id": dl_resp.get("job_id"),
        "download_type": safe_type,
        "quality": chosen_quality,
        "matched_item": {
            "title": title,
            "artist": matched_artist,
            "album_id": album_id,
            "track_id": track_id,
            "hires": best_match.get("hires", False),
            "year": best_match.get("release_year"),
        },
    }


if __name__ == "__main__":
    mcp.run()
