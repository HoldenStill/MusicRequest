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
    assert len(playlist.tracks) > 0
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
