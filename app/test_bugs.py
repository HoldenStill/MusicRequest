"""Unit tests for MusicRequest bugs: Artist Details null handling (R1) & Jellyfin Playlist Cover Art (R2)."""

import asyncio
import os
import shutil
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.streamrip_handler import StreamripHandler
from app.playlist_handler import PlaylistImporter, PlaylistInfo, PlaylistTrack, _get_safe_playlist_name


def make_mock_response(status_code, json_data):
    """Helper to construct an aiohttp context-manager compatible response mock."""
    resp = MagicMock()
    resp.status = status_code
    resp.json = AsyncMock(return_value=json_data)
    resp.text = AsyncMock(return_value="")
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=resp)
    cm.__aexit__ = AsyncMock(return_value=None)
    return cm


class TestArtistDetailsNullHandling(unittest.TestCase):
    """Test R1: Artist Details loading with missing/null values from Qobuz API."""

    def setUp(self):
        self.handler = StreamripHandler(config_path="dummy.toml", music_dir="dummy")

    @patch.object(StreamripHandler, "_create_qobuz_client")
    def test_artist_details_all_null_values(self, mock_create_client):
        """Verify get_artist_details handles null values for image, albums, biography, tracks_top."""
        mock_client = MagicMock()
        
        cm1 = make_mock_response(200, {
            "id": 12345,
            "name": "Less Popular Artist",
            "image": None,
            "albums": None,
            "biography": None,
            "picture": None,
            "albums_count": None
        })

        cm2 = make_mock_response(200, {
            "tracks_top": None,
            "tracks": None
        })

        mock_client.session.get = MagicMock(side_effect=[cm1, cm2])
        mock_client.session.close = AsyncMock()
        mock_create_client.return_value = (mock_client, MagicMock())

        result = asyncio.run(self.handler.get_artist_details("12345"))

        self.assertIsNotNone(result)
        self.assertEqual(result["artist_id"], "12345")
        self.assertEqual(result["name"], "Less Popular Artist")
        self.assertEqual(result["image_url"], "")
        self.assertEqual(result["biography"], "")
        self.assertEqual(result["albums"], [])
        self.assertEqual(result["top_tracks"], [])
        self.assertEqual(result["albums_count"], 0)

    @patch.object(StreamripHandler, "_create_qobuz_client")
    def test_artist_details_missing_keys(self, mock_create_client):
        """Verify get_artist_details handles dictionary with keys entirely missing."""
        mock_client = MagicMock()
        
        cm1 = make_mock_response(200, {
            "id": 999,
            "name": "Sparse Artist"
        })

        cm2 = make_mock_response(200, {})

        mock_client.session.get = MagicMock(side_effect=[cm1, cm2])
        mock_client.session.close = AsyncMock()
        mock_create_client.return_value = (mock_client, MagicMock())

        result = asyncio.run(self.handler.get_artist_details("999"))

        self.assertIsNotNone(result)
        self.assertEqual(result["name"], "Sparse Artist")
        self.assertEqual(result["albums"], [])
        self.assertEqual(result["top_tracks"], [])

    @patch.object(StreamripHandler, "_create_qobuz_client")
    def test_artist_details_list_format_response(self, mock_create_client):
        """Verify get_artist_details handles albums and top_tracks returned directly as lists."""
        mock_client = MagicMock()

        cm1 = make_mock_response(200, {
            "id": 888,
            "name": "List Artist",
            "albums": [
                {"id": "alb1", "title": "Album One", "released_at": "2020-01-01", "image": {"large": "http://img/1_230.jpg"}}
            ],
            "biography": {"content": "Great artist bio"}
        })

        cm2 = make_mock_response(200, {
            "tracks_top": [
                {"id": "trk1", "title": "Track One", "duration": 180, "album": {"id": "alb1", "title": "Album One"}}
            ]
        })

        mock_client.session.get = MagicMock(side_effect=[cm1, cm2])
        mock_client.session.close = AsyncMock()
        mock_create_client.return_value = (mock_client, MagicMock())

        result = asyncio.run(self.handler.get_artist_details("888"))

        self.assertIsNotNone(result)
        self.assertEqual(len(result["albums"]), 1)
        self.assertEqual(result["albums"][0]["title"], "Album One")
        self.assertEqual(result["albums"][0]["release_year"], "2020")
        self.assertEqual(len(result["top_tracks"]), 1)
        self.assertEqual(result["top_tracks"][0]["title"], "Track One")
        self.assertEqual(result["biography"], "Great artist bio")

    def test_extract_cover_art_robustness(self):
        """Test _extract_cover_art helper with nulls and unexpected types."""
        self.assertEqual(StreamripHandler._extract_cover_art(None), "")
        self.assertEqual(StreamripHandler._extract_cover_art({}), "")
        self.assertEqual(StreamripHandler._extract_cover_art({"large": None, "small": None}), "")
        self.assertEqual(StreamripHandler._extract_cover_art({"large": 123}), "")
        self.assertEqual(
            StreamripHandler._extract_cover_art({"large": "http://example.com/img_230.jpg"}),
            "http://example.com/img_600.jpg"
        )


class TestJellyfinPlaylistCoverArt(unittest.TestCase):
    """Test R2: Playlist cover art naming alignment for Jellyfin scanner."""

    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.handler = StreamripHandler(config_path="dummy.toml", music_dir=self.temp_dir)
        self.importer = PlaylistImporter(
            music_dir=self.temp_dir,
            config_path="dummy.toml",
            handler=self.handler
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_safe_playlist_name_sanitization(self):
        """Test playlist name sanitization for filesystem and Jellyfin alignment."""
        self.assertEqual(_get_safe_playlist_name("My/Playlist: Best Of?"), "My_Playlist_ Best Of_")
        self.assertEqual(_get_safe_playlist_name("  Rock & Roll. "), "Rock & Roll")
        self.assertEqual(_get_safe_playlist_name(""), "Imported Playlist")
        self.assertEqual(_get_safe_playlist_name("   "), "Imported Playlist")

    @patch("aiohttp.ClientSession.get")
    def test_download_cover_art_jellyfin_naming(self, mock_get):
        """Verify cover art is downloaded as both {Playlist Name}.jpg and cover.jpg."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "image/jpeg"}
        mock_resp.read = AsyncMock(return_value=b"fake_jpeg_binary_data")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_get.return_value = cm

        playlist = PlaylistInfo(
            source="spotify",
            playlist_id="test1234",
            name="Chill Vibes",
            cover_art_url="https://example.com/cover.jpg"
        )

        asyncio.run(self.importer._download_cover_art(playlist))

        playlist_dir = os.path.join(self.temp_dir, "Playlists", "Chill Vibes")
        primary_cover = os.path.join(playlist_dir, "Chill Vibes.jpg")
        fallback_cover = os.path.join(playlist_dir, "cover.jpg")

        self.assertTrue(os.path.exists(primary_cover), "Primary cover {Playlist Name}.jpg should exist for Jellyfin")
        self.assertTrue(os.path.exists(fallback_cover), "Fallback cover.jpg should exist")

        with open(primary_cover, "rb") as f:
            self.assertEqual(f.read(), b"fake_jpeg_binary_data")

    @patch("aiohttp.ClientSession.get")
    def test_playlist_cover_art_special_characters(self, mock_get):
        """Verify playlist with special characters (slashes, colons, dots) generates matching Jellyfin cover and m3u8 files."""
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.headers = {"Content-Type": "image/png"}
        mock_resp.read = AsyncMock(return_value=b"fake_png_data")
        cm = MagicMock()
        cm.__aenter__ = AsyncMock(return_value=mock_resp)
        cm.__aexit__ = AsyncMock(return_value=None)
        mock_get.return_value = cm

        raw_name = "AC/DC: Live at River Plate."
        expected_safe = "AC_DC_ Live at River Plate"

        playlist = PlaylistInfo(
            source="qobuz",
            playlist_id="acdc123",
            name=raw_name,
            cover_art_url="https://example.com/art.png"
        )

        asyncio.run(self.importer._download_cover_art(playlist))
        self.importer._generate_m3u8_sync(playlist)

        playlist_dir = os.path.join(self.temp_dir, "Playlists", expected_safe)
        primary_png = os.path.join(playlist_dir, f"{expected_safe}.png")
        fallback_png = os.path.join(playlist_dir, "cover.png")
        m3u8_path = os.path.join(playlist_dir, f"{expected_safe}.m3u8")

        self.assertTrue(os.path.exists(primary_png))
        self.assertTrue(os.path.exists(fallback_png))
        self.assertTrue(os.path.exists(m3u8_path))

        with open(m3u8_path, "r", encoding="utf-8") as f:
            content = f.read()
        self.assertIn(f"#EXTIMG:{expected_safe}.png", content)

    def test_generate_m3u8_includes_extimg(self):
        """Verify .m3u8 includes #EXTIMG matching Jellyfin cover image filename."""
        playlist_name = "Synthwave Classics"
        safe_name = _get_safe_playlist_name(playlist_name)
        playlist_dir = os.path.join(self.temp_dir, "Playlists", safe_name)
        os.makedirs(playlist_dir, exist_ok=True)

        # Pre-create the cover image matching Jellyfin convention
        cover_path = os.path.join(playlist_dir, f"{safe_name}.jpg")
        with open(cover_path, "wb") as f:
            f.write(b"fake_cover_data")

        dummy_audio = os.path.join(self.temp_dir, "Artist", "Album", "track1.flac")
        os.makedirs(os.path.dirname(dummy_audio), exist_ok=True)
        with open(dummy_audio, "wb") as f:
            f.write(b"audio_data")

        playlist = PlaylistInfo(
            source="spotify",
            playlist_id="synth123",
            name=playlist_name,
            tracks=[
                PlaylistTrack(
                    title="Track One",
                    artist="Artist One",
                    duration_ms=200000,
                    is_local=True,
                    local_path=dummy_audio
                )
            ]
        )

        self.importer._generate_m3u8_sync(playlist)

        m3u8_file = os.path.join(playlist_dir, f"{safe_name}.m3u8")
        self.assertTrue(os.path.exists(m3u8_file))

        with open(m3u8_file, "r", encoding="utf-8") as f:
            content = f.read()

        self.assertIn("#EXTM3U", content)
        self.assertIn(f"#PLAYLIST:{playlist_name}", content)
        self.assertIn(f"#EXTIMG:{safe_name}.jpg", content)
        self.assertIn("Artist One - Track One", content)


if __name__ == "__main__":
    unittest.main()
