"""Integration and unit tests for the built-in MusicRequest /mcp endpoint."""

import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app import main as app_main
from app import mcp_server


def _parse_mcp_response(resp: Any) -> dict[str, Any]:
    """Parse JSON-RPC payload from either application/json or text/event-stream."""
    content_type = resp.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        for line in resp.text.splitlines():
            if line.startswith("data: "):
                payload = line[len("data: "):].strip()
                if payload:
                    return json.loads(payload)
        raise AssertionError(f"No JSON-RPC data line found in SSE response: {resp.text!r}")
    return resp.json()


def _mcp_headers(token: str, session_id: str | None = None) -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    if session_id:
        headers["Mcp-Session-Id"] = session_id
    return headers


def _init_mcp_session(client: TestClient, token: str) -> str:
    """Initialize an MCP Streamable HTTP session and return the session ID."""
    resp = client.post(
        "/mcp",
        headers=_mcp_headers(token),
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "pytest-client", "version": "1.0.0"},
            },
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "Expected Mcp-Session-Id header on initialize response"

    # Send initialized notification
    notif = client.post(
        "/mcp",
        headers=_mcp_headers(token, session_id),
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        follow_redirects=False,
    )
    assert notif.status_code in (200, 202, 204), notif.text
    return session_id


def _call_tool(
    client: TestClient,
    token: str,
    session_id: str,
    name: str,
    arguments: dict[str, Any],
    req_id: int = 2,
) -> dict[str, Any]:
    resp = client.post(
        "/mcp",
        headers=_mcp_headers(token, session_id),
        json={
            "jsonrpc": "2.0",
            "id": req_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text
    return _parse_mcp_response(resp)


@pytest.fixture
def mock_handler(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """Provide a mocked StreamripHandler and wire it into app.main during TestClient lifespan."""
    handler = MagicMock()
    handler.start_worker = AsyncMock()
    handler.stop_worker = AsyncMock()
    handler.search = AsyncMock(return_value=[])
    handler.get_album_tracks = AsyncMock(return_value=None)
    handler.enqueue_download = AsyncMock(
        return_value=SimpleNamespace(job_id="job-123")
    )
    handler.get_queue_status = MagicMock(
        return_value={"active_job": None, "queued_jobs": [], "completed_jobs": []}
    )
    # Prevent lifespan from spawning a real Qobuz/Streamrip client or touching disk
    monkeypatch.setattr(app_main, "StreamripHandler", lambda **kwargs: handler)
    return handler


# ── Bearer Auth Tests ────────────────────────────────────────────────────────

def test_mcp_auth_missing_and_invalid_tokens(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCP_API_TOKEN", "super-secret-mcp-token")
    init_body = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1.0"},
        },
    }

    with TestClient(app_main.app) as client:
        # Missing Authorization header
        r1 = client.post("/mcp", json=init_body, follow_redirects=False)
        assert r1.status_code == 401
        assert r1.headers.get("www-authenticate") == "Bearer"

        # Wrong scheme
        r2 = client.post(
            "/mcp",
            json=init_body,
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
            follow_redirects=False,
        )
        assert r2.status_code == 401

        # Wrong token
        r3 = client.post(
            "/mcp",
            json=init_body,
            headers={"Authorization": "Bearer wrong-token"},
            follow_redirects=False,
        )
        assert r3.status_code == 401


def test_mcp_auth_fail_closed_when_unconfigured(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCP_API_TOKEN", "")
    monkeypatch.setenv("APP_PASSCODE", "")

    with TestClient(app_main.app) as client:
        resp = client.post(
            "/mcp",
            headers=_mcp_headers("1099"),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )
        assert resp.status_code == 401


def test_mcp_auth_fallback_to_app_passcode(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.setenv("APP_PASSCODE", "fallback-passcode-99")

    with TestClient(app_main.app) as client:
        session_id = _init_mcp_session(client, "fallback-passcode-99")
        assert session_id


def test_mcp_auth_prefers_mcp_api_token_over_passcode(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("MCP_API_TOKEN", "dedicated-mcp-token")
    monkeypatch.setenv("APP_PASSCODE", "1099")

    with TestClient(app_main.app) as client:
        # Fallback passcode should be rejected when MCP_API_TOKEN is set
        r_bad = client.post(
            "/mcp",
            headers=_mcp_headers("1099"),
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
            follow_redirects=False,
        )
        assert r_bad.status_code == 401

        # Dedicated token succeeds
        session_id = _init_mcp_session(client, "dedicated-mcp-token")
        assert session_id


# ── MCP Tools End-to-End Tests ───────────────────────────────────────────────

def test_mcp_list_and_call_all_tools(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    token = "test-mcp-token"
    monkeypatch.setenv("MCP_API_TOKEN", token)

    mock_handler.search.side_effect = [
        # Call 1: musicrequest_search
        [
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
                "cover_art_url": "https://img.qobuz.com/ram.jpg",
            }
        ],
        # Call 2: musicrequest_quick_download (match)
        [
            {
                "album_id": "wrong-alb",
                "title": "Random Access",
                "artist": "Other Artist",
                "hires": False,
            },
            {
                "album_id": "0060253738012",
                "title": "Random Access Memories",
                "artist": "Daft Punk",
                "release_year": 2013,
                "hires": True,
                "cover_art_url": "https://img.qobuz.com/ram.jpg",
            },
        ],
        # Call 3: musicrequest_quick_download (not found)
        [],
    ]
    mock_handler.get_album_tracks.return_value = {
        "album_id": "0060253738012",
        "title": "Random Access Memories",
        "artist": "Daft Punk",
        "release_year": 2013,
        "tracks": [
            {
                "track_id": "trk-1",
                "track_number": 1,
                "title": "Give Life Back to Music",
                "duration_seconds": 274,
                "hires": True,
            }
        ],
    }
    mock_handler.get_queue_status.return_value = {
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
        "completed_jobs": [],
    }

    with TestClient(app_main.app) as client:
        session_id = _init_mcp_session(client, token)

        # 1. tools/list
        list_resp = client.post(
            "/mcp",
            headers=_mcp_headers(token, session_id),
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        assert list_resp.status_code == 200
        tools = _parse_mcp_response(list_resp)["result"]["tools"]
        tool_names = {t["name"] for t in tools}
        assert tool_names == {
            "musicrequest_search",
            "musicrequest_get_album_tracks",
            "musicrequest_download",
            "musicrequest_queue_status",
            "musicrequest_quick_download",
        }

        # 2. musicrequest_search
        search_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_search",
            {"query": "Daft Punk", "type": "album", "limit": 5},
            req_id=3,
        )
        search_data = json.loads(search_rpc["result"]["content"][0]["text"])
        if isinstance(search_data, dict):
            search_data = [search_data]
        assert search_data[0]["album_id"] == "0060253738012"
        assert search_data[0]["quality_spec"] == "24-bit/88.2kHz"
        assert "cover_art_url" not in search_data[0]

        # 3. musicrequest_get_album_tracks
        tracks_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_get_album_tracks",
            {"album_id": "0060253738012"},
            req_id=4,
        )
        tracks_data = json.loads(tracks_rpc["result"]["content"][0]["text"])
        assert tracks_data["total_tracks"] == 1
        assert tracks_data["tracks"][0]["title"] == "Give Life Back to Music"

        # 4. musicrequest_download
        dl_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_download",
            {
                "album_id": "0060253738012",
                "title": "Random Access Memories",
                "artist": "Daft Punk",
                "quality": 3,
            },
            req_id=5,
        )
        dl_data = json.loads(dl_rpc["result"]["content"][0]["text"])
        assert dl_data["status"] == "queued"
        assert dl_data["job_id"] == "job-123"

        # 5. musicrequest_queue_status (full and active_only)
        q_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_queue_status",
            {"active_only": False},
            req_id=6,
        )
        q_data = json.loads(q_rpc["result"]["content"][0]["text"])
        assert q_data["active_job"]["title"] == "Discovery"
        assert len(q_data["queued_jobs"]) == 1

        q_active_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_queue_status",
            {"active_only": True},
            req_id=7,
        )
        q_active = json.loads(q_active_rpc["result"]["content"][0]["text"])
        assert q_active["queued_count"] == 1
        assert "queued_jobs" not in q_active

        # 6. musicrequest_quick_download (artist match)
        qd_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_quick_download",
            {"query": "Random Access Memories", "artist": "Daft Punk", "quality": 3},
            req_id=8,
        )
        qd_data = json.loads(qd_rpc["result"]["content"][0]["text"])
        assert qd_data["status"] == "queued"
        assert qd_data["matched_item"]["album_id"] == "0060253738012"
        assert qd_data["matched_item"]["artist"] == "Daft Punk"

        # 7. musicrequest_quick_download (not found)
        qd_miss_rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_quick_download",
            {"query": "NonExistentAlbumXYZ"},
            req_id=9,
        )
        qd_miss = json.loads(qd_miss_rpc["result"]["content"][0]["text"])
        assert qd_miss["status"] == "error"


def test_mcp_null_handler_guard(
    mock_handler: MagicMock, monkeypatch: pytest.MonkeyPatch
):
    token = "test-mcp-token"
    monkeypatch.setenv("MCP_API_TOKEN", token)

    with TestClient(app_main.app) as client:
        session_id = _init_mcp_session(client, token)
        # Simulate uninitialized handler
        monkeypatch.setattr(app_main, "handler", None)

        rpc = _call_tool(
            client,
            token,
            session_id,
            "musicrequest_search",
            {"query": "Daft Punk"},
            req_id=10,
        )
        assert rpc["result"]["isError"] is True
        assert "not initialized" in rpc["result"]["content"][0]["text"]
