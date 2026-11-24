"""Unit tests for MusicRequest MCP Server."""

import importlib.util
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Load mcp/musicrequest_mcp.py dynamically to avoid namespace shadowing
_MCP_PATH = Path(__file__).resolve().parent.parent / "mcp" / "musicrequest_mcp.py"
_spec = importlib.util.spec_from_file_location("musicrequest_mcp", str(_MCP_PATH))
mr_mcp = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(mr_mcp)


@pytest.fixture(autouse=True)
def reset_mcp_state():
    """Reset cached session cookie between tests."""
    mr_mcp._cached_session_cookie = None
    mr_mcp.MUSICREQUEST_URL = "http://test-server:8080"
    mr_mcp.MUSICREQUEST_PASSCODE = "test_passcode"
    mr_mcp.MUSICREQUEST_DEFAULT_QUALITY = None
    yield
    mr_mcp._cached_session_cookie = None


# ── Compacting Helpers Tests ─────────────────────────────────────────────────

def test_compact_search_item_album():
    raw_album = {
        "type": "album",
        "album_id": "alb-123",
        "title": "Random Access Memories",
        "artist": "Daft Punk",
        "release_year": 2013,
        "tracks_count": 13,
        "duration_seconds": 4464,
        "maximum_bit_depth": 24,
        "maximum_sampling_rate": 88.2,
        "hires": True,
        "cover_art_url": "https://img.qobuz.com/alb123.jpg",
        "extra_verbose_field": "ignore_me",
    }
    compact = mr_mcp._compact_search_item(raw_album)
    assert compact["album_id"] == "alb-123"
    assert compact["title"] == "Random Access Memories"
    assert compact["artist"] == "Daft Punk"
    assert compact["year"] == 2013
    assert compact["tracks"] == 13
    assert compact["hires"] is True
    assert compact["quality_spec"] == "24-bit/88.2kHz"
    assert "extra_verbose_field" not in compact


def test_compact_search_item_track():
    raw_track = {
        "type": "track",
        "track_id": "trk-456",
        "title": "Get Lucky",
        "artist": "Daft Punk",
        "album": "Random Access Memories",
        "album_id": "alb-123",
        "duration_seconds": 369,
        "track_number": 8,
        "hires": True,
    }
    compact = mr_mcp._compact_search_item(raw_track)
    assert compact["track_id"] == "trk-456"
    assert compact["title"] == "Get Lucky"
    assert compact["artist"] == "Daft Punk"
    assert compact["track_no"] == 8
    assert compact["duration_sec"] == 369
    assert compact["hires"] is True


def test_compact_search_item_artist():
    raw_artist = {
        "type": "artist",
        "artist_id": "art-789",
        "name": "Daft Punk",
        "albums_count": 12,
        "picture_url": "https://img.qobuz.com/art789.jpg",
    }
    compact = mr_mcp._compact_search_item(raw_artist)
    assert compact["artist_id"] == "art-789"
    assert compact["name"] == "Daft Punk"
    assert compact["albums_count"] == 12


def test_compact_job():
    raw_job = {
        "job_id": "job-abc",
        "album_id": "alb-123",
        "title": "Discovery",
        "artist": "Daft Punk",
        "status": "downloading",
        "download_type": "album",
        "quality": 3,
        "error": None,
        "queued_at": "2026-11-23T12:00:00",
        "internal_worker_ref": "<Worker>",
    }
    compact = mr_mcp._compact_job(raw_job)
    assert compact["job_id"] == "job-abc"
    assert compact["status"] == "downloading"
    assert compact["quality"] == 3
    assert "internal_worker_ref" not in compact


def test_get_configured_quality():
    mr_mcp.MUSICREQUEST_DEFAULT_QUALITY = 2
    # Explicit quality takes precedence
    assert mr_mcp._get_configured_quality(4) == 4
    # None falls back to default
    assert mr_mcp._get_configured_quality(None) == 2

    mr_mcp.MUSICREQUEST_DEFAULT_QUALITY = None
    assert mr_mcp._get_configured_quality(None) is None


# ── Auth & Request Tests ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_auth_cookie_success():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.cookies = {"session_token": "valid_token_xyz"}
    mock_client.post.return_value = mock_response

    cookie = await mr_mcp._get_auth_cookie(mock_client)
    assert cookie == "valid_token_xyz"
    assert mr_mcp._cached_session_cookie == "valid_token_xyz"
    mock_client.post.assert_called_once_with(
        "http://test-server:8080/api/auth",
        json={"passcode": "test_passcode"},
    )


@pytest.mark.asyncio
async def test_get_auth_cookie_invalid_passcode():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 401
    mock_client.post.return_value = mock_response

    with pytest.raises(RuntimeError, match="Failed to authenticate with MusicRequest"):
        await mr_mcp._get_auth_cookie(mock_client)


@pytest.mark.asyncio
async def test_get_auth_cookie_missing_cookie():
    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_response = MagicMock(spec=httpx.Response)
    mock_response.status_code = 200
    mock_response.cookies = {}
    mock_response.headers = {}
    mock_client.post.return_value = mock_response

    with pytest.raises(RuntimeError, match="no session_token cookie was received"):
        await mr_mcp._get_auth_cookie(mock_client)


@pytest.mark.asyncio
async def test_api_request_auto_retry_on_401():
    """Verify that a 401 response forces a token refresh and retries once."""
    mr_mcp._cached_session_cookie = "expired_token"

    mock_resp_401 = MagicMock(spec=httpx.Response)
    mock_resp_401.status_code = 401

    mock_resp_200 = MagicMock(spec=httpx.Response)
    mock_resp_200.status_code = 200
    mock_resp_200.json.return_value = {"result": "success"}

    mock_auth_resp = MagicMock(spec=httpx.Response)
    mock_auth_resp.status_code = 200
    mock_auth_resp.cookies = {"session_token": "new_fresh_token"}

    mock_client = AsyncMock(spec=httpx.AsyncClient)
    mock_client.request.side_effect = [mock_resp_401, mock_resp_200]
    mock_client.post.return_value = mock_auth_resp

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client_cls.return_value.__aenter__.return_value = mock_client
        data = await mr_mcp._api_request("GET", "/api/queue/status")

    assert data == {"result": "success"}
    assert mr_mcp._cached_session_cookie == "new_fresh_token"
    assert mock_client.request.call_count == 2


# ── MCP Tool Execution Tests ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_musicrequest_search():
    sample_albums = [
        {
            "type": "album",
            "album_id": "0060253738012",
            "title": "Random Access Memories",
            "artist": "Daft Punk",
            "release_year": 2013,
            "tracks_count": 13,
            "hires": True,
            "maximum_bit_depth": 24,
            "maximum_sampling_rate": 88.2,
        }
    ]

    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = sample_albums
        results = await mr_mcp.musicrequest_search(query="Daft Punk", type="album", limit=5)

    assert len(results) == 1
    assert results[0]["title"] == "Random Access Memories"
    assert results[0]["quality_spec"] == "24-bit/88.2kHz"
    mock_req.assert_called_once_with(
        method="GET",
        path="/api/search",
        params={"q": "Daft Punk", "type": "album", "limit": 5},
    )


@pytest.mark.asyncio
async def test_musicrequest_get_album_tracks():
    sample_album_info = {
        "album_id": "alb-123",
        "title": "Discovery",
        "artist": "Daft Punk",
        "release_year": 2001,
        "tracks": [
            {
                "track_id": "trk-1",
                "track_number": 1,
                "title": "One More Time",
                "duration_seconds": 320,
                "hires": False,
            },
            {
                "track_id": "trk-2",
                "track_number": 2,
                "title": "Aerodynamic",
                "duration_seconds": 212,
                "hires": False,
            },
        ],
    }

    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = sample_album_info
        details = await mr_mcp.musicrequest_get_album_tracks("alb-123")

    assert details["album_id"] == "alb-123"
    assert details["title"] == "Discovery"
    assert details["total_tracks"] == 2
    assert len(details["tracks"]) == 2
    assert details["tracks"][0]["title"] == "One More Time"


@pytest.mark.asyncio
async def test_musicrequest_download():
    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = {"job_id": "job-999", "status": "queued"}
        res = await mr_mcp.musicrequest_download(
            album_id="alb-123",
            title="Discovery",
            artist="Daft Punk",
            download_type="album",
            quality=3,
        )

    assert res["status"] == "queued"
    assert res["job_id"] == "job-999"
    mock_req.assert_called_once_with(
        method="POST",
        path="/api/download",
        json_data={
            "album_id": "alb-123",
            "title": "Discovery",
            "artist": "Daft Punk",
            "download_type": "album",
            "track_id": None,
            "quality": 3,
            "cover_art_url": "",
        },
    )


@pytest.mark.asyncio
async def test_musicrequest_queue_status():
    queue_data = {
        "active_job": {
            "job_id": "job-1",
            "title": "Discovery",
            "artist": "Daft Punk",
            "status": "downloading",
            "download_type": "album",
        },
        "queued_jobs": [
            {
                "job_id": "job-2",
                "title": "Homework",
                "artist": "Daft Punk",
                "status": "queued",
                "download_type": "album",
            }
        ],
        "completed_jobs": [
            {
                "job_id": "job-0",
                "title": "Alive 2007",
                "artist": "Daft Punk",
                "status": "completed",
                "download_type": "album",
            }
        ],
    }

    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = queue_data
        full_status = await mr_mcp.musicrequest_queue_status(active_only=False)

    assert full_status["active_job"]["title"] == "Discovery"
    assert len(full_status["queued_jobs"]) == 1
    assert len(full_status["completed_jobs"]) == 1

    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = queue_data
        active_status = await mr_mcp.musicrequest_queue_status(active_only=True)

    assert active_status["active_job"]["title"] == "Discovery"
    assert active_status["queued_count"] == 1
    assert active_status["completed_count"] == 1
    assert "queued_jobs" not in active_status


@pytest.mark.asyncio
async def test_musicrequest_quick_download_with_artist_match():
    search_results = [
        {
            "album_id": "wrong-artist",
            "title": "Random Access",
            "artist": "Some Other Artist",
            "hires": False,
        },
        {
            "album_id": "correct-alb",
            "title": "Random Access Memories",
            "artist": "Daft Punk",
            "hires": True,
            "release_year": 2013,
            "cover_art_url": "https://img.qobuz.com/cov.jpg",
        },
    ]

    async def mock_api(method: str, path: str, **kwargs):
        if path == "/api/search":
            return search_results
        if path == "/api/download":
            return {"job_id": "job-auto-1", "status": "queued"}
        return {}

    with patch.object(mr_mcp, "_api_request", side_effect=mock_api):
        result = await mr_mcp.musicrequest_quick_download(
            query="Random Access Memories",
            artist="Daft Punk",
            download_type="album",
            quality=3,
        )

    assert result["status"] == "queued"
    assert result["job_id"] == "job-auto-1"
    assert result["matched_item"]["album_id"] == "correct-alb"
    assert result["matched_item"]["artist"] == "Daft Punk"
    assert result["matched_item"]["hires"] is True


@pytest.mark.asyncio
async def test_musicrequest_quick_download_not_found():
    with patch.object(mr_mcp, "_api_request", new_callable=AsyncMock) as mock_req:
        mock_req.return_value = []
        result = await mr_mcp.musicrequest_quick_download(
            query="NonExistentAlbumXYZ123",
            download_type="album",
        )

    assert result["status"] == "error"
    assert "No album matches found" in result["message"]
