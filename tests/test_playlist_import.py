import asyncio
import json
import pytest
from app.playlist_handler import PlaylistImporter, PlaylistTrack, SPOTIFY_PLAYLIST_RE


def test_spotify_url_regex():
    urls = [
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M?si=12345", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/intl-de/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/intl-es/playlist/37i9dQZF1DXcBWIGoYBM5M?si=abcdef", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/intl-pt-br/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/user/alice/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/embed/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("spotify:playlist:37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("open.spotify.com/playlist/37i9dQZF1DXcBWIGoYBM5M", "37i9dQZF1DXcBWIGoYBM5M"),
        ("https://open.spotify.com/playlist/4O0n9UYw9v6GbluyjFLODb", "4O0n9UYw9v6GbluyjFLODb"),
    ]
    for url, expected in urls:
        source, pid = PlaylistImporter.parse_playlist_url(url)
        assert source == "spotify", f"Failed source for {url}"
        assert pid == expected, f"Failed pid for {url}: got {pid}, expected {expected}"


@pytest.mark.asyncio
async def test_fetch_spotify_playlist_live():
    importer = PlaylistImporter("/tmp/music", "/tmp/config", None)
    # Test a public 1000-song playlist
    playlist = await importer._fetch_spotify_playlist("4O0n9UYw9v6GbluyjFLODb")
    assert playlist.source == "spotify"
    assert playlist.name == "1000 songs, 1000 artists"
    assert len(playlist.tracks) > 100, f"Expected >100 tracks, got {len(playlist.tracks)}"
    assert playlist.tracks[0].title
    assert playlist.tracks[0].artist


@pytest.mark.asyncio
async def test_fetch_spotify_playlist_private_or_404():
    importer = PlaylistImporter("/tmp/music", "/tmp/config", None)
    # Test a non-existent or deleted playlist ID
    with pytest.raises(RuntimeError) as exc_info:
        await importer._fetch_spotify_playlist("5ABHKGoOzxKaGA29Mfagvf")
    err = str(exc_info.value)
    assert "not found" in err.lower() or "public" in err.lower()


@pytest.mark.asyncio
async def test_fetch_spotify_playlist_mock_structures(monkeypatch):
    importer = PlaylistImporter("/tmp/music", "/tmp/config", None)

    # Mock response with alternative structure (flat pageProps.entity)
    mock_payload = {
        "props": {
            "pageProps": {
                "entity": {
                    "name": "Flat Entity Playlist",
                    "subtitle": "Curator",
                    "trackList": [
                        {"title": "Track 1", "subtitle": "Artist 1", "duration": 180000},
                        None,  # null track resilience
                        {"title": "", "subtitle": "No Title"},  # invalid track
                        {"title": "Track 2", "subtitle": "Artist 2", "duration": 200000},
                    ]
                }
            }
        }
    }
    mock_html = f'<html><script id="__NEXT_DATA__" type="application/json">{json.dumps(mock_payload)}</script></html>'

    class MockResponse:
        status = 200
        async def text(self):
            return mock_html
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    class MockSession:
        def get(self, url, headers=None):
            return MockResponse()
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass

    monkeypatch.setattr("aiohttp.ClientSession", MockSession)

    result = await importer._fetch_spotify_playlist("test_mock_id")
    assert result.name == "Flat Entity Playlist"
    assert len(result.tracks) == 2
    assert result.tracks[0].title == "Track 1"
    assert result.tracks[1].title == "Track 2"


@pytest.mark.asyncio
async def test_analyze_playlist_concurrent_matching(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.session = MagicMock()
    mock_client.session.close = AsyncMock()

    mock_handler = MagicMock()
    mock_handler._create_qobuz_client = AsyncMock(return_value=(mock_client, MagicMock()))

    async def mock_search_with_client(client, query, limit=10, media_type="track"):
        parts = query.split()
        artist = parts[0] if parts else "Artist"
        title = " ".join(parts[1:]) if len(parts) > 1 else "Track"
        return [{
            "type": "track",
            "track_id": "12345",
            "album_id": "67890",
            "title": title,
            "artist": artist,
            "duration": 180,
        }]

    mock_handler.search_with_client = AsyncMock(side_effect=mock_search_with_client)

    importer = PlaylistImporter("/tmp/music", "/tmp/config", mock_handler)

    # Mock fetch spotify playlist to return 10 tracks
    async def mock_fetch(pid):
        from app.playlist_handler import PlaylistInfo, PlaylistTrack
        tracks = [
            PlaylistTrack(title=f"Track {i}", artist=f"Artist {i}", duration_ms=180000)
            for i in range(10)
        ]
        return PlaylistInfo(
            source="spotify",
            playlist_id=pid,
            name="Test Batch Playlist",
            tracks=tracks,
        )

    monkeypatch.setattr(importer, "_fetch_spotify_playlist", mock_fetch)
    monkeypatch.setattr(importer, "_check_duplicates", AsyncMock())

    result = await importer.analyze_playlist("https://open.spotify.com/playlist/test_batch")
    assert len(result.tracks) == 10
    assert mock_handler._create_qobuz_client.call_count == 1
    assert mock_handler.search_with_client.call_count >= 10
    assert result.tracks[0].qobuz_track_id == "12345"


@pytest.mark.asyncio
async def test_start_analysis_async_job(monkeypatch):
    from unittest.mock import AsyncMock, MagicMock

    mock_client = MagicMock()
    mock_client.session = MagicMock()
    mock_client.session.close = AsyncMock()

    mock_handler = MagicMock()
    mock_handler._create_qobuz_client = AsyncMock(return_value=(mock_client, MagicMock()))

    async def mock_search_with_client(client, query, limit=10, media_type="track"):
        return [{
            "type": "track",
            "track_id": "999",
            "album_id": "888",
            "title": "Track 0",
            "artist": "Artist 0",
            "duration": 180,
        }]

    mock_handler.search_with_client = AsyncMock(side_effect=mock_search_with_client)

    importer = PlaylistImporter("/tmp/music", "/tmp/config", mock_handler)

    async def mock_fetch(pid):
        from app.playlist_handler import PlaylistInfo, PlaylistTrack
        return PlaylistInfo(
            source="spotify",
            playlist_id=pid,
            name="Async Test Playlist",
            tracks=[PlaylistTrack(title="Track 0", artist="Artist 0", duration_ms=180000)],
        )

    monkeypatch.setattr(importer, "_fetch_spotify_playlist", mock_fetch)
    monkeypatch.setattr(importer, "_check_duplicates", AsyncMock())

    job = importer.start_analysis("https://open.spotify.com/playlist/test_async")
    assert job.analysis_id
    assert job.status in ("pending", "scraping", "completed")

    sub_q = importer.subscribe_analysis(job.analysis_id)
    # Wait for completion
    for _ in range(50):
        if job.status in ("completed", "failed"):
            break
        await asyncio.sleep(0.05)

    assert job.status == "completed"
    assert job.result is not None
    assert job.result["name"] == "Async Test Playlist"
    assert job.playlist_info is not None
    assert job.progress == 100
    importer.unsubscribe_analysis(job.analysis_id, sub_q)


@pytest.mark.asyncio
async def test_api_analyze_and_status(monkeypatch):
    import app.main as main_mod
    from app.main import PlaylistAnalyzeRequest, analyze_playlist, playlist_analysis_status
    from unittest.mock import MagicMock
    from app.playlist_handler import AnalysisJob

    mock_importer = MagicMock()
    dummy_job = AnalysisJob(analysis_id="test-123", url="https://open.spotify.com/playlist/dummy", status="pending")
    mock_importer.parse_playlist_url = MagicMock(return_value=("spotify", "dummy"))
    mock_importer.start_analysis = MagicMock(return_value=dummy_job)
    mock_importer.analysis_jobs = {"test-123": dummy_job}

    monkeypatch.setattr(main_mod, "playlist_importer", mock_importer)

    # 1. Test POST /api/playlist/analyze returns immediately
    req = PlaylistAnalyzeRequest(url="https://open.spotify.com/playlist/dummy")
    res = await analyze_playlist(req)
    assert res == {"analysis_id": "test-123", "status": "pending"}

    # 2. Test GET /api/playlist/analysis/{id}/status
    status_res = await playlist_analysis_status("test-123")
    assert status_res["analysis_id"] == "test-123"
    assert status_res["status"] == "pending"


