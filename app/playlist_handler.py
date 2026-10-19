"""Playlist Importer & Migration Tool for MusicRequest.

Supports scraping public Spotify playlists (zero-setup) and Qobuz playlists,
checking for local duplicates, downloading missing tracks, and generating
.m3u8 playlist files with cover art.
"""

from __future__ import annotations

import asyncio
import base64
import gc
import json
import logging
import os
import pyotp
import re
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TYPE_CHECKING

import aiohttp

if TYPE_CHECKING:
    from app.streamrip_handler import StreamripHandler

logger = logging.getLogger(__name__)

# ── Regex for URL detection ──────────────────────────────────────────────────
SPOTIFY_PLAYLIST_RE = re.compile(
    r"(?:https?://)?(?:open\.)?spotify\.com/(?:(?:intl-[a-z]{2}(?:-[a-zA-Z0-9]+)?|user/[^/]+)/)?(?:embed/)?playlist/([a-zA-Z0-9]+)|spotify:playlist:([a-zA-Z0-9]+)",
    re.IGNORECASE,
)
QOBUZ_PLAYLIST_RE = re.compile(
    r"(?:https?://)?(?:open|play|www)\.qobuz\.com/.*playlist/([a-zA-Z0-9-]+)"
)

# ── Normalization helpers ────────────────────────────────────────────────────
_BRACKET_RE = re.compile(r"\s*[\(\[\{].*?[\)\]\}]\s*")
_FEAT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring)\s+.*", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|;|\\|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b)\s*",
    re.IGNORECASE,
)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:remaster(?:ed)?(?:\s*\d+)?|radio edit|explicit(?: version)?|single version|album version|live(?: at .*)?|acoustic|bonus track|from\s+.*|theme from\s+.*|motion picture\s+.*|soundtrack\s+.*|original mix|extended mix|club mix|feat\.?\s+.*|ft\.?\s+.*).*$",
    re.IGNORECASE,
)


def _normalize(text: str) -> str:
    """Normalize a string for fuzzy matching.

    - Lowercase
    - Strip accents
    - Remove bracketed content: (Remastered 2011), [Deluxe Edition]
    - Remove 'feat.' / 'ft.' suffixes
    - Remove non-alphanumeric characters
    - Collapse whitespace
    """
    if not text or not isinstance(text, str):
        return ""
    text = text.lower().strip()
    # Strip accents
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    # Strip bracketed content
    text = _BRACKET_RE.sub("", text)
    # Strip feat. / ft.
    text = _FEAT_RE.sub("", text)
    # Remove non-alphanumeric
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    return text


def _clean_text_basic(text: str) -> str:
    """Basic clean without stripping brackets or words (lowercase, accents, non-alnum)."""
    if not text or not isinstance(text, str):
        return ""
    text = text.lower().strip()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = _NON_ALNUM_RE.sub(" ", text)
    text = _MULTI_SPACE_RE.sub(" ", text).strip()
    return text


def _extract_artist_variants(artist_str: str) -> list[str]:
    """Extract individual artist names and combinations from an artist string."""
    if not artist_str or not isinstance(artist_str, str):
        return []

    variants: set[str] = set()
    raw_norm = _normalize(artist_str)
    if raw_norm:
        variants.add(raw_norm)
        if raw_norm.startswith("the "):
            variants.add(raw_norm[4:].strip())

    basic_norm = _clean_text_basic(artist_str)
    if basic_norm:
        variants.add(basic_norm)
        if basic_norm.startswith("the "):
            variants.add(basic_norm[4:].strip())

    # Split by delimiters (commas, &, feat, etc.)
    parts = _ARTIST_SPLIT_RE.split(artist_str)
    for part in parts:
        part_norm = _normalize(part)
        if part_norm:
            variants.add(part_norm)
            if part_norm.startswith("the "):
                variants.add(part_norm[4:].strip())

    # If first part exists, also add it as primary
    if parts:
        primary = _normalize(parts[0])
        if primary:
            variants.add(primary)
            if primary.startswith("the "):
                variants.add(primary[4:].strip())

    return [v for v in variants if v]


def _get_title_variants(title_str: str) -> list[str]:
    """Generate title variations (full, without brackets, without suffix, etc.)."""
    if not title_str or not isinstance(title_str, str):
        return []

    variants: set[str] = set()
    raw_norm = _normalize(title_str)
    if raw_norm:
        variants.add(raw_norm)

    basic_norm = _clean_text_basic(title_str)
    if basic_norm:
        variants.add(basic_norm)

    # Strip suffix after hyphen (e.g., " - Remastered 2011", " - Radio Edit")
    suffix_stripped = _TITLE_SUFFIX_RE.sub("", title_str)
    s_norm = _normalize(suffix_stripped)
    if s_norm:
        variants.add(s_norm)

    # Also plain bracket-stripped
    bracket_stripped = _BRACKET_RE.sub("", title_str)
    b_norm = _normalize(bracket_stripped)
    if b_norm:
        variants.add(b_norm)

    b_suffix = _clean_text_basic(suffix_stripped)
    if b_suffix:
        variants.add(b_suffix)

    return [v for v in variants if v]


def _make_lookup_keys(artist: str, title: str) -> list[str]:
    """Generate all normalized lookup key permutations for an artist + title pair."""
    artist_vars = _extract_artist_variants(artist)
    title_vars = _get_title_variants(title)

    keys: list[str] = []
    for a in artist_vars:
        for t in title_vars:
            key = f"{a}::{t}"
            if key not in keys:
                keys.append(key)
    return keys


def _get_safe_playlist_name(name: str) -> str:
    """Sanitize playlist name for directory and file paths, stripping invalid characters and trailing dots/spaces."""
    if not name or not isinstance(name, str):
        return "Imported Playlist"
    safe = re.sub(r'[<>:"/\\|?*]', '_', name).strip().rstrip(".")
    return safe if safe else "Imported Playlist"


def _make_index_key(artist: str, title: str) -> str:
    """Create a primary normalized lookup key from artist + title."""
    return f"{_normalize(artist)}::{_normalize(title)}"


def _format_m3u8_relative_path(audio_path_str: str, music_dir: str, playlist_dir: Path) -> str:
    """Format an audio file path as a clean relative path from playlist_dir for Jellyfin/m3u8."""
    clean_audio = audio_path_str.replace("\\", "/").rstrip("/")
    clean_music = str(music_dir).replace("\\", "/").rstrip("/")

    # Check if audio path starts with music_dir
    if clean_audio.startswith(clean_music + "/"):
        subpath = clean_audio[len(clean_music) + 1:]
        return f"../../{subpath}"

    # Check if audio path came from Jellyfin with common mount prefixes
    for prefix in ["/music", "/media/music", "/media", "/mnt/music", "/data/music"]:
        if prefix and clean_audio.startswith(prefix + "/"):
            subpath = clean_audio[len(prefix) + 1:]
            return f"../../{subpath}"

    # Standard relpath calculation
    try:
        rel = os.path.relpath(Path(audio_path_str), playlist_dir).replace("\\", "/")
        return rel
    except Exception:
        return clean_audio


# ── Data Classes ─────────────────────────────────────────────────────────────
@dataclass
class PlaylistTrack:
    """A single track from a parsed playlist."""
    title: str
    artist: str
    duration_ms: int = 0
    album_title: str = ""
    cover_art_url: str = ""
    isrc: str = ""
    # Populated after analysis
    is_local: bool = False
    local_path: str = ""
    qobuz_track_id: str = ""
    qobuz_album_id: str = ""
    matched_title: str = ""
    matched_artist: str = ""
    match_confidence: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "artist": self.artist,
            "duration_ms": self.duration_ms,
            "album_title": self.album_title,
            "cover_art_url": self.cover_art_url,
            "is_local": self.is_local,
            "local_path": self.local_path,
            "qobuz_track_id": self.qobuz_track_id,
            "qobuz_album_id": self.qobuz_album_id,
            "matched_title": self.matched_title,
            "matched_artist": self.matched_artist,
            "match_confidence": self.match_confidence,
        }


@dataclass
class PlaylistInfo:
    """Metadata about a parsed playlist."""
    source: str  # "spotify" or "qobuz"
    playlist_id: str
    name: str
    description: str = ""
    owner: str = ""
    cover_art_url: str = ""
    tracks: list[PlaylistTrack] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        local_count = sum(1 for t in self.tracks if t.is_local)
        missing_count = len(self.tracks) - local_count
        return {
            "source": self.source,
            "playlist_id": self.playlist_id,
            "name": self.name,
            "description": self.description,
            "owner": self.owner,
            "cover_art_url": self.cover_art_url,
            "total_tracks": len(self.tracks),
            "local_tracks": local_count,
            "missing_tracks": missing_count,
            "tracks": [t.to_dict() for t in self.tracks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PlaylistInfo:
        """Reconstruct a PlaylistInfo object from a dictionary."""
        tracks = [
            PlaylistTrack(
                title=t.get("title", ""),
                artist=t.get("artist", ""),
                duration_ms=t.get("duration_ms", 0),
                album_title=t.get("album_title", ""),
                cover_art_url=t.get("cover_art_url", ""),
                isrc=t.get("isrc", ""),
                is_local=t.get("is_local", False),
                local_path=t.get("local_path", ""),
                qobuz_track_id=t.get("qobuz_track_id", ""),
                qobuz_album_id=t.get("qobuz_album_id", ""),
                matched_title=t.get("matched_title", ""),
                matched_artist=t.get("matched_artist", ""),
                match_confidence=t.get("match_confidence", 0.0),
            )
            for t in data.get("tracks", [])
        ]
        return cls(
            source=data.get("source", "spotify"),
            playlist_id=data.get("playlist_id", ""),
            name=data.get("name", "Untitled"),
            description=data.get("description", ""),
            owner=data.get("owner", ""),
            cover_art_url=data.get("cover_art_url", ""),
            tracks=tracks,
        )


@dataclass
class AnalysisJob:
    """Tracks the state of an asynchronous playlist analysis operation."""
    analysis_id: str
    url: str
    status: str = "pending"  # pending, scraping, indexing, matching, completed, failed
    progress: int = 0        # 0 to 100 percentage
    current_step: str = "Initializing analysis..."
    result: dict[str, Any] | None = None
    playlist_info: PlaylistInfo | None = None
    error: str = ""
    created_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "analysis_id": self.analysis_id,
            "status": self.status,
            "progress": self.progress,
            "current_step": self.current_step,
            "error": self.error,
            "result": self.result,
        }


@dataclass
class ImportJob:
    """Tracks the state of a playlist import operation."""
    import_id: str
    playlist_name: str
    total_tracks: int
    completed_tracks: int = 0
    failed_tracks: int = 0
    status: str = "analyzing"  # analyzing, matching, downloading, generating, complete, failed
    current_step: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "import_id": self.import_id,
            "playlist_name": self.playlist_name,
            "total_tracks": self.total_tracks,
            "completed_tracks": self.completed_tracks,
            "failed_tracks": self.failed_tracks,
            "status": self.status,
            "current_step": self.current_step,
            "error": self.error,
        }


# ── Main Importer ────────────────────────────────────────────────────────────
class PlaylistImporter:
    """Orchestrates playlist import: scrape → index → match → download → m3u8."""

    def __init__(
        self,
        music_dir: str,
        config_path: str,
        handler: StreamripHandler,
        jellyfin_url: str = "",
        jellyfin_api_key: str = "",
    ):
        self.music_dir = music_dir
        self.config_path = config_path
        self.handler = handler
        self.jellyfin_url = jellyfin_url.rstrip("/") if jellyfin_url else ""
        self.jellyfin_api_key = jellyfin_api_key

        # Active analysis jobs & subscribers
        self.analysis_jobs: dict[str, AnalysisJob] = {}
        self._analysis_subscribers: dict[str, set[asyncio.Queue[dict[str, Any]]]] = {}

        # Active import jobs
        self.import_jobs: dict[str, ImportJob] = {}
        self._import_subscribers: dict[str, set[asyncio.Queue]] = {}

        # Cached library index: (timestamp, index_dict)
        self._cached_library_index: tuple[float, dict[str, Any]] | None = None
        self._library_cache_ttl: float = 300.0  # 5 minutes

        # Spotify Web Player auth state & hash (zero-setup pagination)
        self._spotify_access_token: str | None = None
        self._spotify_token_expires_at: float = 0.0
        self._spotify_client_token: str | None = None
        self._spotify_client_version: str = "1.3.7.496.ga9aacef93081"
        self._spotify_device_id: str = "00000000-0000-0000-0000-000000000000"
        self._spotify_fetch_playlist_hash: str = "21ea11a6ac75a5f3e01fd365d29c18e0a4a8b7dc09f89bbb5d73910190c6acaf"

    # ── URL Parsing ──────────────────────────────────────────────────────────
    @staticmethod
    def parse_playlist_url(url: str) -> tuple[str, str]:
        """Parse a playlist URL and return (source, playlist_id).

        Raises ValueError if the URL is not recognized.
        """
        url = url.strip()
        m = SPOTIFY_PLAYLIST_RE.search(url)
        if m:
            pid = m.group(1) or m.group(2)
            return "spotify", pid
        m = QOBUZ_PLAYLIST_RE.search(url)
        if m:
            return "qobuz", m.group(1)
        raise ValueError(
            "Unrecognized playlist URL. Please paste a Spotify or Qobuz playlist link."
        )

    # ── Spotify Scraping ─────────────────────────────────────────────────────
    async def _get_spotify_auth(self, session: aiohttp.ClientSession) -> tuple[str, str, str]:
        """Obtain or reuse cached Spotify Web Player accessToken and clientToken."""
        now = time.time()
        if (
            self._spotify_access_token
            and self._spotify_client_token
            and self._spotify_client_version
            and now < (self._spotify_token_expires_at - 60)
        ):
            return self._spotify_access_token, self._spotify_client_token, self._spotify_client_version

        # 1. Fetch open.spotify.com to get clientVersion and device ID
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
        }
        async with session.get("https://open.spotify.com", headers=headers) as resp:
            if resp.status == 200:
                html = await resp.text()
                match = re.search(r'<script id="appServerConfig" type="text/plain">(.*?)</script>', html)
                if match:
                    try:
                        cfg = json.loads(base64.b64decode(match.group(1)).decode("utf-8"))
                        if cfg.get("clientVersion"):
                            self._spotify_client_version = cfg["clientVersion"]
                    except Exception:
                        pass
                for cookie in session.cookie_jar:
                    if cookie.key == "sp_t":
                        self._spotify_device_id = cookie.value
                        break

        if not self._spotify_client_version:
            self._spotify_client_version = "1.3.7.496.ga9aacef93081"
        if not self._spotify_device_id:
            self._spotify_device_id = str(uuid.uuid4())

        # 2. Get TOTP access token + client_id
        version = 18
        secret_bytes = bytearray([44, 55, 47, 42, 70, 40, 34, 114, 76, 74, 50, 111, 120, 97, 75, 76, 94, 102, 43, 69, 49, 120, 118, 80, 64, 78])
        transformed = [e ^ ((t % 33) + 9) for t, e in enumerate(secret_bytes)]
        hex_str = "".join(str(num) for num in transformed).encode().hex()
        secret = base64.b32encode(bytes.fromhex(hex_str)).decode().rstrip("=")
        totp = pyotp.TOTP(secret).now()

        tok_url = (
            f"https://open.spotify.com/api/token?reason=init&productType=web-player"
            f"&totp={totp}&totpVer={version}&totpServer={totp}"
        )
        async with session.get(tok_url, headers=headers) as resp:
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"Spotify token endpoint failed: HTTP {resp.status} - {txt[:100]}")
            tok_data = json.loads(await resp.text())

        access_token = tok_data.get("accessToken")
        client_id = tok_data.get("clientId")
        expires_ms = tok_data.get("accessTokenExpirationTimestampMs") or (now * 1000 + 3600 * 1000)
        if not access_token or not client_id:
            raise RuntimeError("Missing accessToken or clientId from Spotify token response")

        # 3. Get client token
        ct_url = "https://clienttoken.spotify.com/v1/clienttoken"
        ct_payload = {
            "client_data": {
                "client_version": self._spotify_client_version,
                "client_id": client_id,
                "js_sdk_data": {
                    "device_brand": "unknown",
                    "device_model": "unknown",
                    "os": "windows",
                    "os_version": "NT 10.0",
                    "device_id": self._spotify_device_id,
                    "device_type": "computer",
                },
            }
        }
        ct_headers = {"Content-Type": "application/json", "Accept": "application/json"}
        async with session.post(ct_url, json=ct_payload, headers=ct_headers) as resp:
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"Spotify clienttoken endpoint failed: HTTP {resp.status} - {txt[:100]}")
            ct_data = json.loads(await resp.text())

        client_token = ct_data.get("granted_token", {}).get("token")
        if not client_token:
            raise RuntimeError(f"Missing client token in response: {ct_data}")

        self._spotify_access_token = access_token
        self._spotify_client_token = client_token
        self._spotify_token_expires_at = float(expires_ms) / 1000.0

        return access_token, client_token, self._spotify_client_version

    async def _fetch_spotify_pathfinder(self, playlist_id: str) -> PlaylistInfo:
        """Fetch complete Spotify playlist via Pathfinder GraphQL with pagination."""
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Referer": "https://open.spotify.com/",
        }

        async with aiohttp.ClientSession(headers=headers) as session:
            access_tok, client_tok, client_ver = await self._get_spotify_auth(session)

            query_url = "https://api-partner.spotify.com/pathfinder/v1/query"
            query_headers = {
                "Authorization": f"Bearer {access_tok}",
                "Client-Token": client_tok,
                "Spotify-App-Version": client_ver,
                "Content-Type": "application/json;charset=UTF-8",
                "Accept": "application/json",
            }

            offset = 0
            limit = 343  # Maximum limit supported by Spotify pathfinder query
            total_count = None
            playlist_name = ""
            description = ""
            owner = ""
            cover_art_url = ""
            tracks: list[PlaylistTrack] = []

            while total_count is None or offset < total_count:
                params = {
                    "operationName": "fetchPlaylist",
                    "variables": json.dumps({
                        "uri": f"spotify:playlist:{playlist_id}",
                        "offset": offset,
                        "limit": limit,
                        "enableWatchFeedEntrypoint": False,
                    }),
                    "extensions": json.dumps({
                        "persistedQuery": {
                            "version": 1,
                            "sha256Hash": self._spotify_fetch_playlist_hash,
                        }
                    }),
                }

                async with session.post(query_url, headers=query_headers, params=params) as resp:
                    if resp.status in (404, 400):
                        txt = await resp.text()
                        if "not found" in txt.lower() or "invalid" in txt.lower():
                            raise RuntimeError(
                                f"Spotify playlist '{playlist_id}' was not found. "
                                "Please ensure the playlist is set to Public on Spotify."
                            )
                        raise RuntimeError(f"Spotify Pathfinder API returned HTTP {resp.status}: {txt[:200]}")
                    elif resp.status != 200:
                        txt = await resp.text()
                        raise RuntimeError(f"Spotify Pathfinder API returned HTTP {resp.status}: {txt[:200]}")

                    res = json.loads(await resp.text())

                pv2 = res.get("data", {}).get("playlistV2")
                if not pv2:
                    raise RuntimeError("Spotify returned empty playlist data")

                if not playlist_name:
                    playlist_name = pv2.get("name") or "Untitled Playlist"
                    description = pv2.get("description") or ""
                    owner = pv2.get("ownerV2", {}).get("data", {}).get("name") or ""
                    images_items = pv2.get("images", {}).get("items", [])
                    if images_items and isinstance(images_items, list):
                        sources = images_items[0].get("sources", [])
                        if sources and isinstance(sources, list):
                            cover_art_url = sources[0].get("url") or ""

                content = pv2.get("content", {})
                if total_count is None:
                    total_count = content.get("totalCount", 0)

                items = content.get("items", [])
                if not items:
                    break

                for item in items:
                    data = item.get("itemV2", {}).get("data", {})
                    title = data.get("name")
                    if not title:
                        continue

                    duration_ms = data.get("trackDuration", {}).get("totalMilliseconds", 0) or 0
                    artists_items = data.get("artists", {}).get("items", [])
                    artists = [
                        a.get("profile", {}).get("name")
                        for a in artists_items
                        if isinstance(a, dict) and a.get("profile", {}).get("name")
                    ]
                    artist = ", ".join(filter(None, artists))

                    cover_sources = data.get("albumOfTrack", {}).get("coverArt", {}).get("sources", [])
                    track_cover = cover_sources[0].get("url", "") if cover_sources else ""

                    tracks.append(PlaylistTrack(
                        title=title,
                        artist=artist,
                        duration_ms=duration_ms,
                        cover_art_url=track_cover or cover_art_url,
                    ))

                offset += len(items)
                if len(items) == 0 or offset >= total_count:
                    break

            if not tracks:
                raise RuntimeError(f"Spotify playlist '{playlist_id}' yielded 0 tracks")

            logger.info(
                f"Successfully parsed Spotify playlist '{playlist_name}' ({playlist_id}) "
                f"with {len(tracks)} tracks (total reported: {total_count})"
            )

            return PlaylistInfo(
                source="spotify",
                playlist_id=playlist_id,
                name=playlist_name,
                description=description,
                owner=owner,
                cover_art_url=cover_art_url,
                tracks=tracks,
            )

    async def _fetch_spotify_playlist(self, playlist_id: str) -> PlaylistInfo:
        """Fetch complete playlist metadata + tracks from Spotify.

        Primary method: Spotify Web Player Pathfinder GraphQL (supports full pagination
        of all tracks, up to thousands).
        Fallback method: Spotify embed page scraper (first 100 tracks).
        """
        try:
            info = await self._fetch_spotify_pathfinder(playlist_id)
            if info and info.tracks:
                return info
        except Exception as e:
            err_msg = str(e)
            if "not found" in err_msg.lower() or "public" in err_msg.lower():
                raise
            logger.warning(
                f"Pathfinder API failed for Spotify playlist '{playlist_id}' ({e}); "
                "falling back to embed page scraper."
            )

        return await self._fetch_spotify_embed(playlist_id)

    async def _fetch_spotify_embed(self, playlist_id: str) -> PlaylistInfo:
        """Scrape a Spotify embed page to extract playlist metadata + tracks."""
        url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "en-US,en;q=0.9",
        }

        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status != 200:
                    raise RuntimeError(
                        f"Spotify returned HTTP {resp.status} for playlist {playlist_id}"
                    )
                html = await resp.text()

        # Extract __NEXT_DATA__ JSON from the embed page
        match = re.search(
            r'<script[^>]*id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>',
            html,
            re.DOTALL,
        )
        if not match:
            if "Page not found" in html:
                raise RuntimeError(
                    f"Spotify playlist '{playlist_id}' was not found. "
                    "Please check that the link is correct and the playlist is set to Public."
                )
            raise RuntimeError(
                "Could not find __NEXT_DATA__ in Spotify embed page. "
                "The playlist may be private or the page structure changed."
            )

        try:
            data = json.loads(match.group(1))
        except json.JSONDecodeError as e:
            raise RuntimeError(f"Failed to parse Spotify embed data as JSON: {e}")

        # Check for error status or 'Page not found' in pageProps
        page_props = data.get("props", {}).get("pageProps", {})
        if isinstance(page_props, dict):
            status = page_props.get("status")
            title = page_props.get("title", "")
            if status in (404, 403) or "not found" in str(title).lower():
                raise RuntimeError(
                    f"Spotify returned '{title or 'Not Found'}' for playlist '{playlist_id}'. "
                    "If this is a personal playlist, please ensure it is set to Public on Spotify."
                )

        # Navigate the JSON tree to find the entity object across known paths
        entity = None
        if isinstance(page_props, dict):
            entity = (
                page_props.get("state", {}).get("data", {}).get("entity")
                or page_props.get("entity")
                or page_props.get("state", {}).get("entity")
                or page_props.get("data", {}).get("entity")
                or page_props.get("initialState", {}).get("entity")
            )

        # Fallback: search anywhere in data for an entity dict containing trackList or playlist data
        if not entity or not isinstance(entity, dict):
            def _find_entity_in_obj(obj: Any) -> dict | None:
                if isinstance(obj, dict):
                    if ("trackList" in obj and isinstance(obj["trackList"], list)) or (
                        obj.get("type") in ("playlist", "album") and ("tracks" in obj or "trackList" in obj)
                    ):
                        return obj
                    for v in obj.values():
                        res = _find_entity_in_obj(v)
                        if res:
                            return res
                elif isinstance(obj, list):
                    for item in obj:
                        res = _find_entity_in_obj(item)
                        if res:
                            return res
                return None

            entity = _find_entity_in_obj(data)

        if not entity or not isinstance(entity, dict):
            logger.error(
                f"Failed to extract entity for playlist {playlist_id}. "
                f"PageProps keys: {list(page_props.keys()) if isinstance(page_props, dict) else type(page_props)}"
            )
            raise RuntimeError(
                f"Could not extract playlist data from Spotify for '{playlist_id}'. "
                "The playlist may be private, empty, or region-restricted."
            )

        playlist_name = (
            entity.get("name")
            or entity.get("title")
            or "Untitled Playlist"
        )
        description = entity.get("description", "") or ""
        owner = (
            entity.get("subtitle")
            or (entity.get("owner", {}).get("display_name") if isinstance(entity.get("owner"), dict) else "")
            or ""
        )

        # Cover art — try images array, coverArt, or visualIdentity
        cover_art_url = ""
        images = entity.get("images", [])
        if isinstance(images, list) and images:
            valid_images = [img for img in images if isinstance(img, dict)]
            if valid_images:
                best = max(valid_images, key=lambda img: img.get("width", 0) or 0)
                cover_art_url = best.get("url", "")
        if not cover_art_url:
            cover_art = entity.get("coverArt") or entity.get("visualIdentity", {})
            if isinstance(cover_art, dict):
                sources = cover_art.get("sources", [])
                if isinstance(sources, list) and sources:
                    valid_sources = [s for s in sources if isinstance(s, dict)]
                    if valid_sources:
                        best = max(valid_sources, key=lambda s: s.get("width", 0) or 0)
                        cover_art_url = best.get("url", "")

        # Parse tracks (supports trackList, tracks, items in embed or API schemas)
        tracks: list[PlaylistTrack] = []
        raw_tracks = (
            entity.get("trackList")
            or entity.get("tracks")
            or entity.get("items")
            or []
        )
        if isinstance(raw_tracks, dict):
            raw_tracks = raw_tracks.get("items") or []
        if not isinstance(raw_tracks, list):
            raw_tracks = []

        for item in raw_tracks:
            if not isinstance(item, dict):
                continue

            track_obj = item.get("track") if isinstance(item.get("track"), dict) else item

            title = (
                track_obj.get("title")
                or track_obj.get("name")
                or ""
            )

            artist = track_obj.get("subtitle") or track_obj.get("artist") or ""
            if not artist:
                artists_list = track_obj.get("artists")
                if isinstance(artists_list, list):
                    artist = ", ".join(
                        a.get("name", "") for a in artists_list if isinstance(a, dict) and a.get("name")
                    )

            if not title:
                continue

            duration_ms = track_obj.get("duration", 0) or track_obj.get("duration_ms", 0) or 0

            # Track cover art
            track_cover = ""
            cover_art_data = track_obj.get("coverArt") or track_obj.get("album", {}).get("images", [])
            if isinstance(cover_art_data, dict):
                sources = cover_art_data.get("sources", [])
                if isinstance(sources, list) and sources and isinstance(sources[0], dict):
                    track_cover = sources[0].get("url", "")
            elif isinstance(cover_art_data, list) and cover_art_data and isinstance(cover_art_data[0], dict):
                track_cover = cover_art_data[0].get("url", "")

            tracks.append(PlaylistTrack(
                title=title,
                artist=artist,
                duration_ms=duration_ms,
                cover_art_url=track_cover or cover_art_url,
            ))

        if not tracks:
            logger.warning(f"Spotify playlist '{playlist_id}' parsed but yielded 0 tracks")

        return PlaylistInfo(
            source="spotify",
            playlist_id=playlist_id,
            name=playlist_name,
            description=description,
            owner=owner,
            cover_art_url=cover_art_url,
            tracks=tracks,
        )

    # ── Qobuz Playlist Fetch ────────────────────────────────────────────────
    async def _fetch_qobuz_playlist(self, playlist_id: str) -> PlaylistInfo:
        """Fetch a Qobuz playlist via the authenticated API."""
        client = None
        try:
            client, sr_config = await self.handler._create_qobuz_client()

            url = "https://www.qobuz.com/api.json/0.2/playlist/get"
            params = {
                "playlist_id": playlist_id,
                "extra": "tracks",
                "limit": 500,
            }

            async with client.session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise RuntimeError(
                        f"Qobuz API returned {resp.status}: {text}"
                    )
                data = await resp.json()
        except Exception:
            if client and client.session:
                await client.session.close()
            raise
        else:
            if client and client.session:
                await client.session.close()

        # Parse
        playlist_name = data.get("name", "Untitled")
        description = data.get("description", "")
        owner = data.get("owner", {}).get("name", "")
        cover_art_url = ""
        images = data.get("images300", [])
        if images:
            cover_art_url = images[0] if isinstance(images[0], str) else ""
        if not cover_art_url:
            image_data = data.get("image", {})
            if isinstance(image_data, dict):
                cover_art_url = image_data.get("large", "") or image_data.get("small", "") or ""

        tracks: list[PlaylistTrack] = []
        raw_tracks = data.get("tracks", {}).get("items", [])
        for item in raw_tracks:
            title = item.get("title", "")
            if not title:
                continue

            performer = item.get("performer", {}) or {}
            artist = performer.get("name", "Unknown")
            album = item.get("album", {}) or {}
            album_title = album.get("title", "")
            album_id = str(album.get("id", ""))
            duration_ms = (item.get("duration", 0) or 0) * 1000  # Qobuz returns seconds

            track_cover = ""
            album_image = album.get("image", {})
            if isinstance(album_image, dict):
                track_cover = album_image.get("large", "") or album_image.get("small", "") or ""

            tracks.append(PlaylistTrack(
                title=title,
                artist=artist,
                duration_ms=duration_ms,
                album_title=album_title,
                cover_art_url=track_cover or cover_art_url,
                qobuz_track_id=str(item.get("id", "")),
                qobuz_album_id=album_id,
                matched_title=title,
                matched_artist=artist,
                is_local=False,
            ))

        return PlaylistInfo(
            source="qobuz",
            playlist_id=playlist_id,
            name=playlist_name,
            description=description,
            owner=owner,
            cover_art_url=cover_art_url,
            tracks=tracks,
        )

    # ── Library Indexing (Cached with TTL) ───────────────────────────────────
    async def _build_library_index(self, force_refresh: bool = False) -> dict[str, Any]:
        """Scan the music directory for audio files, read tags with mutagen,
        and return a structured index with direct keys and title fallback mapping.

        Caches the index in memory for 5 minutes so repeated analyses do not re-scan disk.
        """
        now = time.time()
        if not force_refresh and self._cached_library_index is not None:
            cached_time, cached_index = self._cached_library_index
            if (now - cached_time) < self._library_cache_ttl:
                logger.info(
                    f"Using cached library index ({int(now - cached_time)}s old, "
                    f"{len(cached_index.get('keys', {}))} lookup keys)"
                )
                return cached_index

        index = await asyncio.to_thread(self._scan_library_sync)
        self._cached_library_index = (now, index)
        return index

    def invalidate_library_cache(self) -> None:
        """Invalidate the cached library index so the next lookup rescans disk."""
        self._cached_library_index = None

    def _scan_library_sync(self) -> dict[str, Any]:
        """Synchronous library scanner using mutagen and filesystem fallbacks."""
        import mutagen

        keys_index: dict[str, str] = {}
        titles_index: dict[str, list[tuple[str, str]]] = {}
        all_files: list[str] = []
        music_path = Path(self.music_dir)
        if not music_path.exists():
            logger.warning(f"Music directory does not exist: {self.music_dir}")
            return {"keys": keys_index, "titles": titles_index, "files": all_files}

        audio_exts = {".flac", ".mp3", ".ogg", ".m4a", ".opus", ".wma", ".wav", ".aac"}
        scanned = 0
        errors = 0

        for root, _, files in os.walk(str(music_path), followlinks=True):
            for file in files:
                ext = os.path.splitext(file)[1].lower()
                if ext not in audio_exts:
                    continue
                filepath = os.path.join(root, file)
                all_files.append(filepath)
                try:
                    titles: list[str] = []
                    artists: list[str] = []

                    # 1. Try reading tags with mutagen
                    tags = None
                    try:
                        tags = mutagen.File(filepath, easy=True)
                    except Exception:
                        pass

                    if tags is not None and hasattr(tags, "get"):
                        for t_key in ["title", "TITLE", "TIT2", "\xa9nam"]:
                            try:
                                raw_t = tags.get(t_key, [])
                            except Exception:
                                raw_t = []
                            if raw_t:
                                for item in (raw_t if isinstance(raw_t, (list, tuple)) else [raw_t]):
                                    if isinstance(item, str) and item.strip() and item.strip() not in titles:
                                        titles.append(item.strip())
                        for a_key in ["artist", "ARTIST", "albumartist", "ALBUMARTIST", "TPE1", "TPE2", "\xa9ART", "aART"]:
                            try:
                                raw_a = tags.get(a_key, [])
                            except Exception:
                                raw_a = []
                            if raw_a:
                                for item in (raw_a if isinstance(raw_a, (list, tuple)) else [raw_a]):
                                    if isinstance(item, str) and item.strip() and item.strip() not in artists:
                                        artists.append(item.strip())

                    # 2. Fallback: Parse from filename and folder structure
                    filename_no_ext = os.path.splitext(file)[0]
                    # Strip leading track number e.g. "01 - " or "01 " or "1. "
                    clean_file_title = re.sub(r"^\d+[\s\.\-_]+", "", filename_no_ext).strip()
                    if clean_file_title and clean_file_title not in titles:
                        titles.append(clean_file_title)

                    parent_folder = os.path.basename(root)
                    grandparent_folder = os.path.basename(os.path.dirname(root))
                    if grandparent_folder and grandparent_folder not in ("music", "Music", "Playlists", ""):
                        if grandparent_folder not in artists:
                            artists.append(grandparent_folder)
                    if parent_folder and parent_folder not in ("music", "Music", "Playlists", ""):
                        if parent_folder not in artists:
                            artists.append(parent_folder)

                    if not titles:
                        continue

                    path_str = str(filepath)
                    for t in titles:
                        for a in artists:
                            for k in _make_lookup_keys(a, t):
                                keys_index[k] = path_str
                            for t_var in _get_title_variants(t):
                                norm_var = _normalize(t_var)
                                if norm_var:
                                    if norm_var not in titles_index:
                                        titles_index[norm_var] = []
                                    titles_index[norm_var].append((a, path_str))

                    scanned += 1
                except Exception:
                    errors += 1
                    continue

        logger.info(
            f"Library scan complete: {scanned} tracks indexed, {errors} errors, "
            f"{len(keys_index)} lookup keys generated"
        )
        return {"keys": keys_index, "titles": titles_index, "files": all_files}

    # ── Jellyfin Library Index ───────────────────────────────────────────────
    async def _build_jellyfin_index(self) -> dict[str, Any] | None:
        """Query Jellyfin API for all audio items and build a structured index.
        Returns None if Jellyfin is not configured or the query fails.
        """
        if not self.jellyfin_url or not self.jellyfin_api_key:
            return None

        try:
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            params = {
                "IncludeItemTypes": "Audio",
                "Recursive": "true",
                "Fields": "Path",
                "Limit": "100000",
            }
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self.jellyfin_url}/Items",
                    headers=headers, params=params,
                ) as resp:
                    if resp.status != 200:
                        logger.warning(f"Jellyfin API returned {resp.status}")
                        return None
                    data = await resp.json()

            keys_index: dict[str, str] = {}
            titles_index: dict[str, list[tuple[str, str]]] = {}
            all_files: list[str] = []
            for item in data.get("Items", []):
                title = item.get("Name", "")
                artists = item.get("Artists", [])
                album_artist = item.get("AlbumArtist", "")
                path = item.get("Path", "")
                if path:
                    all_files.append(path)

                all_artists: list[str] = [a for a in artists if isinstance(a, str) and a]
                if album_artist and album_artist not in all_artists:
                    all_artists.append(album_artist)

                if title and all_artists and path:
                    for a in all_artists:
                        for k in _make_lookup_keys(a, title):
                            keys_index[k] = path
                    for t_var in _get_title_variants(title):
                        norm_var = _normalize(t_var)
                        if norm_var:
                            if norm_var not in titles_index:
                                titles_index[norm_var] = []
                            titles_index[norm_var].append((all_artists[0], path))

            logger.info(f"Jellyfin index built: {len(keys_index)} lookup keys")
            return {"keys": keys_index, "titles": titles_index, "files": all_files}
        except Exception as e:
            logger.warning(f"Failed to query Jellyfin: {e}")
            return None

    # ── Track Path Resolution ────────────────────────────────────────────────
    def _resolve_track_path(
        self, track: PlaylistTrack, library_index: dict[str, Any] | None
    ) -> str | None:
        """Find the local audio file path for a track using exact keys and fuzzy aliases."""
        if not library_index:
            return None

        direct_keys: dict[str, str] = library_index.get("keys", library_index)
        title_index: dict[str, list[tuple[str, str]]] = library_index.get("titles", {})

        # 1. Try direct lookup keys from original artist/title (HIGHEST PRIORITY)
        keys_to_try = _make_lookup_keys(track.artist, track.title)
        for k in keys_to_try:
            if k in direct_keys:
                return direct_keys[k]

        # 2. Try direct lookup keys from matched Qobuz artist/title as secondary fallback
        if track.matched_artist or track.matched_title:
            m_artist = track.matched_artist or track.artist
            m_title = track.matched_title or track.title
            m_keys = []
            for k in _make_lookup_keys(m_artist, m_title):
                if k not in keys_to_try and k not in m_keys:
                    m_keys.append(k)
            for k in _make_lookup_keys(track.artist, m_title):
                if k not in keys_to_try and k not in m_keys:
                    m_keys.append(k)
            for k in m_keys:
                if k in direct_keys:
                    return direct_keys[k]

        # 3. Title index exact & variant matching (scored: pick best artist match)
        if title_index:
            title_candidates = _get_title_variants(track.title)
            if track.matched_title:
                for tv in _get_title_variants(track.matched_title):
                    if tv not in title_candidates:
                        title_candidates.append(tv)

            artist_variants = _extract_artist_variants(track.artist)
            m_artist_variants = _extract_artist_variants(track.matched_artist) if track.matched_artist else []

            best_path: str | None = None
            best_artist_score: float = 0.0

            for t_var in title_candidates:
                norm_t = _normalize(t_var)
                if not norm_t:
                    continue
                candidates = title_index.get(norm_t, [])
                for cand_artist, cand_path in candidates:
                    cand_artist_norm = _normalize(cand_artist)
                    # Check original artist variants first (weight 1.0)
                    for a_var in artist_variants:
                        score = 0.0
                        if a_var == cand_artist_norm:
                            score = 1.0
                        elif len(a_var) >= 3 and len(cand_artist_norm) >= 3:
                            longer = max(len(a_var), len(cand_artist_norm))
                            shorter = min(len(a_var), len(cand_artist_norm))
                            if a_var in cand_artist_norm or cand_artist_norm in a_var:
                                ratio = shorter / longer
                                if ratio >= 0.6:
                                    score = ratio
                        if score > best_artist_score:
                            best_artist_score = score
                            best_path = cand_path

                    # Check matched artist variants as secondary fallback (weight 0.85)
                    for ma_var in m_artist_variants:
                        score = 0.0
                        if ma_var == cand_artist_norm:
                            score = 0.85
                        elif len(ma_var) >= 3 and len(cand_artist_norm) >= 3:
                            longer = max(len(ma_var), len(cand_artist_norm))
                            shorter = min(len(ma_var), len(cand_artist_norm))
                            if ma_var in cand_artist_norm or cand_artist_norm in ma_var:
                                ratio = shorter / longer
                                if ratio >= 0.6:
                                    score = ratio * 0.85
                        if score > best_artist_score:
                            best_artist_score = score
                            best_path = cand_path

            if best_path and best_artist_score >= 0.5:
                return best_path

        # 4. Fuzzy title substring fallback (strict: requires >=5 chars and strong artist validation)
        if title_index:
            clean_titles = [_clean_text_basic(v) for v in (track.title, track.matched_title) if v]
            clean_artists = [_clean_text_basic(v) for v in (track.artist, track.matched_artist) if v]

            best_fuzzy_path: str | None = None
            best_fuzzy_score: float = 0.0

            for ct in clean_titles:
                if len(ct) < 5:
                    continue
                for idx_title, candidates in title_index.items():
                    title_match = False
                    if ct == idx_title:
                        title_match = True
                    elif ct in idx_title or idx_title in ct:
                        longer_t = max(len(ct), len(idx_title))
                        shorter_t = min(len(ct), len(idx_title))
                        if shorter_t / longer_t >= 0.7:
                            title_match = True
                    if not title_match:
                        continue

                    for cand_artist, cand_path in candidates:
                        cand_a_clean = _clean_text_basic(cand_artist)
                        for ca in clean_artists:
                            score = 0.0
                            if ca == cand_a_clean:
                                score = 1.0
                            elif len(ca) >= 3 and len(cand_a_clean) >= 3:
                                if ca in cand_a_clean or cand_a_clean in ca:
                                    longer_a = max(len(ca), len(cand_a_clean))
                                    shorter_a = min(len(ca), len(cand_a_clean))
                                    if shorter_a / longer_a >= 0.5:
                                        score = shorter_a / longer_a
                            if score > best_fuzzy_score:
                                best_fuzzy_score = score
                                best_fuzzy_path = cand_path

            if best_fuzzy_path and best_fuzzy_score >= 0.5:
                return best_fuzzy_path

        # 5. Filepath filename search fallback (strict: match filename AND verify artist folder)
        all_files = library_index.get("files", [])
        if all_files:
            search_title = _clean_text_basic(track.matched_title or track.title)
            clean_artists = [_clean_text_basic(v) for v in (track.artist, track.matched_artist) if v]
            if len(search_title) >= 8:
                for fpath in all_files:
                    fname = _clean_text_basic(os.path.splitext(os.path.basename(fpath))[0])
                    fname = re.sub(r"^\d+[\s.\-_]+", "", fname).strip()
                    if not fname:
                        continue
                    if search_title in fname or fname in search_title:
                        longer_f = max(len(search_title), len(fname))
                        shorter_f = min(len(search_title), len(fname))
                        if shorter_f / longer_f >= 0.6:
                            # Verify that the folder path contains at least one artist variant
                            clean_fpath = _clean_text_basic(fpath)
                            if any(ca in clean_fpath for ca in clean_artists if len(ca) >= 3):
                                return fpath

        return None

    # ── Duplicate Checking ───────────────────────────────────────────────────
    async def _check_duplicates(
        self, tracks: list[PlaylistTrack],
    ) -> dict[str, Any]:
        """Build a library index and check which tracks already exist locally.

        Returns the index for reuse in m3u8 generation.
        """
        # Try Jellyfin first, fall back to local scanner
        index = await self._build_jellyfin_index()
        if index is None:
            index = await self._build_library_index()

        for track in tracks:
            path = self._resolve_track_path(track, index)
            if path:
                track.is_local = True
                track.local_path = path

        return index

    # ── Qobuz Track Matching ────────────────────────────────────────────────
    async def _match_track_on_qobuz(self, track: PlaylistTrack, client: Any = None) -> bool:
        """Search Qobuz for a matching track and populate qobuz_track_id.

        Returns True if a match was found.
        """
        # 1. Clean primary artist and clean title for high-precision search query
        artist_parts = _ARTIST_SPLIT_RE.split(track.artist)
        primary_artist = artist_parts[0].strip() if artist_parts else track.artist.strip()

        # Clean title: strip brackets like "(with Kenny Mason & Project Pat)" or "(feat. ...)"
        clean_title = _BRACKET_RE.sub(" ", track.title).strip()
        clean_title = _TITLE_SUFFIX_RE.sub("", clean_title).strip()
        if not clean_title:
            clean_title = track.title.strip()

        # Try clean query first, fallback to original query if no results
        queries_to_try: list[str] = []
        if primary_artist and clean_title:
            queries_to_try.append(f"{primary_artist} {clean_title}")
        orig_q = f"{track.artist} {track.title}".strip()
        if orig_q not in queries_to_try:
            queries_to_try.append(orig_q)
        if primary_artist and track.title != clean_title:
            queries_to_try.append(f"{primary_artist} {track.title}")

        results: list[dict[str, Any]] = []
        for query in queries_to_try:
            try:
                if client:
                    results = await self.handler.search_with_client(client, query, limit=5, media_type="track")
                else:
                    results = await self.handler.search(query, limit=5, media_type="track")
                if results:
                    break
            except Exception as e:
                logger.warning(f"Qobuz search failed for '{query}': {e}")

        if not results:
            return False

        # Extract all artist variants for the requested track
        track_artist_variants = set(_extract_artist_variants(track.artist))

        # Target duration
        target_duration_s = track.duration_ms / 1000 if track.duration_ms else 0
        best_score = 0.0
        best_result = None

        for result in results:
            r_title = result.get("title", "")
            r_artist = result.get("artist", "")
            r_duration = result.get("duration", 0)

            # Check artist match
            r_artist_variants = set(_extract_artist_variants(r_artist))

            # Exact or shared artist
            common_artists = track_artist_variants.intersection(r_artist_variants)
            if common_artists:
                artist_match = 1.0
            else:
                # Substring/partial artist match
                best_a_sim = 0.0
                for a1 in track_artist_variants:
                    for a2 in r_artist_variants:
                        if not a1 or not a2:
                            continue
                        if a1 == a2:
                            best_a_sim = 1.0
                            break
                        longer = max(len(a1), len(a2))
                        shorter = min(len(a1), len(a2))
                        if shorter >= 3 and (a1 in a2 or a2 in a1):
                            ratio = shorter / longer
                            if ratio > best_a_sim:
                                best_a_sim = ratio
                artist_match = best_a_sim if best_a_sim >= 0.6 else 0.0

            # CRITICAL GUARD: If artist does not match at all, this CANNOT be a match!
            if artist_match <= 0.0:
                continue

            # Title similarity
            t_candidates = _get_title_variants(track.title)
            r_t_candidates = _get_title_variants(r_title)

            title_match = 0.0
            if set(t_candidates).intersection(set(r_t_candidates)):
                title_match = 1.0
            else:
                nt = _normalize(track.title)
                nr = _normalize(r_title)
                if nt and nr:
                    if nt == nr:
                        title_match = 1.0
                    elif nt in nr or nr in nt:
                        longer_t = max(len(nt), len(nr))
                        shorter_t = min(len(nt), len(nr))
                        if shorter_t / longer_t >= 0.6:
                            title_match = 0.85

            if title_match <= 0.0:
                continue

            # Duration similarity (within 6 seconds tolerance)
            duration_diff = abs(r_duration - target_duration_s) if target_duration_s > 0 else 0
            duration_match = 1.0 if duration_diff <= 6 else max(0.0, 1.0 - duration_diff / 30)

            score = (title_match * 0.45) + (artist_match * 0.40) + (duration_match * 0.15)

            if score > best_score:
                best_score = score
                best_result = result

        # Require minimum confidence and strict artist+title match
        if best_result and best_score >= 0.65:
            track.qobuz_track_id = best_result.get("track_id", "")
            track.qobuz_album_id = best_result.get("album_id", "")
            track.matched_title = best_result.get("title", "")
            track.matched_artist = best_result.get("artist", "")
            track.match_confidence = round(best_score, 2)
            return True

        return False

    # ── Asynchronous Playlist Analysis (Non-blocking SSE) ───────────────────
    def start_analysis(self, url: str) -> AnalysisJob:
        """Create and start a background analysis job, returning immediately."""
        analysis_id = str(uuid.uuid4())
        job = AnalysisJob(analysis_id=analysis_id, url=url)
        self.analysis_jobs[analysis_id] = job
        asyncio.create_task(self._run_analysis(job))
        return job

    async def _run_analysis(self, job: AnalysisJob) -> None:
        """Background worker that performs playlist analysis with real-time SSE progress."""
        try:
            job.status = "scraping"
            job.current_step = "Parsing playlist link..."
            job.progress = 5
            self._notify_analysis_subscribers(job)

            source, playlist_id = self.parse_playlist_url(job.url)
            job.current_step = f"Fetching playlist tracks from {source.capitalize()}..."
            job.progress = 15
            self._notify_analysis_subscribers(job)

            # 1. Fetch playlist metadata + tracks
            if source == "spotify":
                playlist = await self._fetch_spotify_playlist(playlist_id)
            else:
                playlist = await self._fetch_qobuz_playlist(playlist_id)

            if not playlist.tracks:
                job.status = "completed"
                job.current_step = "No tracks found in playlist."
                job.progress = 100
                job.result = playlist.to_dict()
                self._notify_analysis_subscribers(job)
                return

            total_tracks = len(playlist.tracks)
            job.status = "indexing"
            job.current_step = f"Checking local library for duplicates ({total_tracks} tracks)..."
            job.progress = 30
            self._notify_analysis_subscribers(job)

            # 2. Check local duplicates (builds/uses cached index)
            await self._check_duplicates(playlist.tracks)

            local_count = sum(1 for t in playlist.tracks if t.is_local)
            missing = [t for t in playlist.tracks if not t.is_local]
            job.current_step = f"Found {local_count} local track(s). Matching {len(missing)} missing track(s)..."
            job.progress = 45
            self._notify_analysis_subscribers(job)

            # 3. Match missing tracks on Qobuz
            if source == "qobuz":
                for track in missing:
                    if track.qobuz_track_id:
                        track.match_confidence = 1.0
                job.progress = 95
            elif missing:
                job.status = "matching"
                client = None
                try:
                    client, sr_config = await self.handler._create_qobuz_client()
                    sem = asyncio.Semaphore(5)
                    matched_done = 0
                    total_missing = len(missing)

                    async def match_one(t: PlaylistTrack) -> None:
                        nonlocal matched_done
                        async with sem:
                            await self._match_track_on_qobuz(t, client=client)
                            matched_done += 1
                            pct = 45 + int((matched_done / total_missing) * 50)
                            job.progress = min(pct, 95)
                            job.current_step = f"Matching tracks on Qobuz: {matched_done} / {total_missing}..."
                            self._notify_analysis_subscribers(job)

                    await asyncio.gather(*(match_one(t) for t in missing), return_exceptions=True)
                except Exception as e:
                    logger.warning(f"Error during batch Qobuz track matching: {e}")
                finally:
                    if client and client.session:
                        await client.session.close()

            gc.collect()

            result_dict = playlist.to_dict()
            job.playlist_info = playlist
            job.status = "completed"
            job.current_step = "Analysis complete!"
            job.progress = 100
            job.result = result_dict
            self._notify_analysis_subscribers(job)

        except Exception as e:
            logger.exception(f"Analysis failed for {job.url}")
            job.status = "failed"
            job.error = str(e)
            job.current_step = f"Analysis failed: {e}"
            job.progress = 100
            self._notify_analysis_subscribers(job)

    # ── Analyze Playlist (Synchronous Helper) ────────────────────────────────
    async def analyze_playlist(self, url: str) -> PlaylistInfo:
        """Full analysis pipeline: parse URL → fetch tracks → check duplicates."""
        source, playlist_id = self.parse_playlist_url(url)

        # Fetch playlist metadata + tracks
        if source == "spotify":
            playlist = await self._fetch_spotify_playlist(playlist_id)
        else:
            playlist = await self._fetch_qobuz_playlist(playlist_id)

        if not playlist.tracks:
            return playlist

        # Check local duplicates (builds and discards index)
        await self._check_duplicates(playlist.tracks)

        # Match missing tracks on Qobuz
        missing = [t for t in playlist.tracks if not t.is_local]
        if source == "qobuz":
            for track in missing:
                if track.qobuz_track_id:
                    track.match_confidence = 1.0
        elif missing:
            client = None
            try:
                client, sr_config = await self.handler._create_qobuz_client()
                sem = asyncio.Semaphore(5)

                async def match_one(t: PlaylistTrack) -> None:
                    async with sem:
                        await self._match_track_on_qobuz(t, client=client)

                await asyncio.gather(*(match_one(t) for t in missing), return_exceptions=True)
            except Exception as e:
                logger.warning(f"Error during batch Qobuz track matching: {e}")
            finally:
                if client and client.session:
                    await client.session.close()

        # Force garbage collection of the library index
        gc.collect()

        return playlist

    # ── Import Playlist (Download Missing) ───────────────────────────────────
    async def import_playlist(
        self,
        playlist: PlaylistInfo,
        selected_indices: list[int] | None = None,
        quality: int = 3,
    ) -> ImportJob:
        """Queue missing tracks for download and generate .m3u8 on completion.

        Args:
            playlist: The analyzed playlist with matched tracks.
            selected_indices: Indices of tracks to download (None = all missing).
            quality: Qobuz quality level (1-4).

        Returns:
            An ImportJob for tracking progress.
        """
        import_id = str(uuid.uuid4())

        # Re-check library right now to catch any tracks added since analysis
        # and to prevent downloading songs that are already local
        fresh_index = await self._build_library_index()
        for track in playlist.tracks:
            if not track.is_local:
                path = self._resolve_track_path(track, fresh_index)
                if path:
                    track.is_local = True
                    track.local_path = path
                    logger.info(f"Track '{track.title}' by '{track.artist}' found locally at import time, skipping download")
        del fresh_index
        gc.collect()

        # Determine which tracks to download
        tracks_to_download: list[PlaylistTrack] = []
        if selected_indices is not None:
            for idx in selected_indices:
                if 0 <= idx < len(playlist.tracks):
                    track = playlist.tracks[idx]
                    if not track.is_local and track.qobuz_track_id:
                        tracks_to_download.append(track)
        else:
            tracks_to_download = [
                t for t in playlist.tracks
                if not t.is_local and t.qobuz_track_id
            ]

        job = ImportJob(
            import_id=import_id,
            playlist_name=playlist.name,
            total_tracks=len(tracks_to_download),
            status="downloading" if tracks_to_download else "generating",
        )
        self.import_jobs[import_id] = job

        # Launch background task
        asyncio.create_task(
            self._run_import(job, playlist, tracks_to_download, quality)
        )

        return job

    async def _run_import(
        self,
        job: ImportJob,
        playlist: PlaylistInfo,
        tracks_to_download: list[PlaylistTrack],
        quality: int,
    ) -> None:
        """Background task: enqueue downloads, wait, then generate m3u8."""
        try:
            # Enqueue all tracks for download
            download_jobs = []
            for i, track in enumerate(tracks_to_download):
                job.current_step = f"Queuing track {i + 1} of {job.total_tracks}: {track.title}"
                self._notify_import_subscribers(job)

                dl_job = await self.handler.enqueue_download(
                    album_id=track.qobuz_album_id,
                    title=track.title,
                    artist=track.artist,
                    download_type="track",
                    track_id=track.qobuz_track_id,
                    quality=quality,
                    cover_art_url=track.cover_art_url,
                )
                download_jobs.append((track, dl_job))

            # Wait for all downloads to complete by polling
            job.current_step = "Waiting for downloads to complete..."
            self._notify_import_subscribers(job)

            while True:
                all_done = True
                completed = 0
                failed = 0
                for track, dl_job in download_jobs:
                    if dl_job.status == "completed":
                        completed += 1
                    elif dl_job.status == "failed":
                        failed += 1
                    else:
                        all_done = False

                job.completed_tracks = completed
                job.failed_tracks = failed
                job.current_step = (
                    f"Downloading: {completed + failed}/{job.total_tracks} "
                    f"({completed} done, {failed} failed)"
                )
                self._notify_import_subscribers(job)

                if all_done:
                    break
                await asyncio.sleep(2)

            # Generate .m3u8 and download cover art
            job.status = "generating"
            job.current_step = "Generating playlist file..."
            self._notify_import_subscribers(job)

            # Re-scan library to find newly downloaded files
            library_index = await self._build_library_index()

            # Update tracks that were missing or were just downloaded
            for track in playlist.tracks:
                if not track.is_local or not track.local_path:
                    path = self._resolve_track_path(track, library_index)
                    if path:
                        track.is_local = True
                        track.local_path = path

            # Ensure all tracks that completed downloading in this batch are resolved
            for track, dl_job in download_jobs:
                if dl_job.status == "completed":
                    path = self._resolve_track_path(track, library_index)
                    if path:
                        track.is_local = True
                        track.local_path = path
                    elif not track.local_path:
                        logger.warning(
                            f"Track '{track.title}' by '{track.artist}' was downloaded but path was not resolved by index."
                        )

            # Free the index
            del library_index
            gc.collect()

            cover_bytes, content_type = await self._download_cover_art(playlist)
            await self._generate_m3u8(playlist)

            # Trigger Jellyfin library scan if configured
            await self._trigger_jellyfin_scan()

            # Upload the cover directly to Jellyfin in the background
            if cover_bytes and content_type:
                asyncio.create_task(
                    self._upload_jellyfin_playlist_cover(playlist.name, cover_bytes, content_type)
                )

            job.status = "complete"
            job.current_step = "Import complete!"
            self._notify_import_subscribers(job)

        except Exception as e:
            logger.exception(f"Import failed for playlist '{playlist.name}'")
            job.status = "failed"
            job.error = str(e)
            job.current_step = f"Failed: {e}"
            self._notify_import_subscribers(job)

    # ── M3U8 Generation ──────────────────────────────────────────────────────
    async def _generate_m3u8(self, playlist: PlaylistInfo) -> None:
        """Generate a .m3u8 file in /music/Playlists/{name}/."""
        await asyncio.to_thread(self._generate_m3u8_sync, playlist)

    def _generate_m3u8_sync(self, playlist: PlaylistInfo) -> None:
        """Synchronous m3u8 generation."""
        # Sanitize playlist name for filesystem using central helper
        safe_name = _get_safe_playlist_name(playlist.name)

        playlist_dir = Path(self.music_dir) / "Playlists" / safe_name
        playlist_dir.mkdir(parents=True, exist_ok=True)

        m3u_path = playlist_dir / f"{safe_name}.m3u8"

        lines = ["#EXTM3U", f"#PLAYLIST:{playlist.name}"]

        # If cover art exists, reference it with #EXTIMG:
        cover_filename = None
        for cand in [
            f"{safe_name}.jpg", f"{safe_name}.png", f"{safe_name}.jpeg", f"{safe_name}.webp",
            "cover.jpg", "cover.png", "cover.jpeg", "cover.webp",
            "folder.jpg", "folder.png"
        ]:
            if (playlist_dir / cand).exists():
                cover_filename = cand
                break

        if cover_filename:
            lines.append(f"#EXTIMG:{cover_filename}")

        added = 0
        skipped = 0
        for track in playlist.tracks:
            if not track.is_local or not track.local_path:
                skipped += 1
                logger.debug(
                    f"M3U8 skip: '{track.title}' by '{track.artist}' "
                    f"(is_local={track.is_local}, local_path='{track.local_path}')"
                )
                continue

            rel_path = _format_m3u8_relative_path(track.local_path, self.music_dir, playlist_dir)
            duration_s = track.duration_ms // 1000 if track.duration_ms else -1
            lines.append(f"#EXTINF:{duration_s},{track.artist} - {track.title}")
            lines.append(rel_path)
            added += 1

        m3u_content = "\n".join(lines) + "\n"
        m3u_path.write_text(m3u_content, encoding="utf-8")
        logger.info(f"Generated playlist file: {m3u_path} ({added} tracks added, {skipped} skipped)")

    # ── Cover Art Download ───────────────────────────────────────────────────
    async def _download_cover_art(self, playlist: PlaylistInfo) -> tuple[bytes | None, str | None]:
        """Download the playlist cover art to /music/Playlists/{name}/.

        Saves image as both {safe_name}{ext} (matching Jellyfin's playlist image naming convention)
        and cover{ext}.
        """
        if not playlist.cover_art_url:
            return None, None

        safe_name = _get_safe_playlist_name(playlist.name)
        playlist_dir = Path(self.music_dir) / "Playlists" / safe_name

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(playlist.cover_art_url) as resp:
                    if resp.status == 200:
                        playlist_dir.mkdir(parents=True, exist_ok=True)
                        data = await resp.read()

                        # Determine file extension from content-type header or URL
                        content_type = resp.headers.get("Content-Type", "").lower()
                        ext = ".jpg"
                        if "png" in content_type or playlist.cover_art_url.lower().endswith(".png"):
                            ext = ".png"
                        elif "webp" in content_type or playlist.cover_art_url.lower().endswith(".webp"):
                            ext = ".webp"

                        primary_cover_path = playlist_dir / f"{safe_name}{ext}"
                        fallback_cover_path = playlist_dir / f"cover{ext}"

                        await asyncio.to_thread(primary_cover_path.write_bytes, data)
                        if primary_cover_path != fallback_cover_path:
                            await asyncio.to_thread(fallback_cover_path.write_bytes, data)

                        logger.info(f"Downloaded cover art: {primary_cover_path}")
                        return data, content_type or "image/jpeg"
                    else:
                        logger.warning(
                            f"Failed to download cover art: HTTP {resp.status}"
                        )
        except Exception as e:
            logger.warning(f"Error downloading cover art: {e}")

        return None, None

    # ── Jellyfin Integration ─────────────────────────────────────────────────
    async def _trigger_jellyfin_scan(self) -> None:
        """Trigger a Jellyfin library scan if configured."""
        if not self.jellyfin_url or not self.jellyfin_api_key:
            return

        try:
            headers = {"X-Emby-Token": self.jellyfin_api_key}
            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.jellyfin_url}/Library/Refresh",
                    headers=headers,
                ) as resp:
                    if resp.status in (200, 204):
                        logger.info("Triggered Jellyfin library scan.")
                    else:
                        logger.warning(
                            f"Jellyfin library scan returned HTTP {resp.status}"
                        )
        except Exception as e:
            logger.warning(f"Failed to trigger Jellyfin scan: {e}")

    async def _upload_jellyfin_playlist_cover(
        self, playlist_name: str, cover_bytes: bytes, mime_type: str
    ) -> None:
        """Search for the playlist in Jellyfin and upload its cover art directly."""
        if not self.jellyfin_url or not self.jellyfin_api_key:
            return

        headers = {"X-Emby-Token": self.jellyfin_api_key}
        search_url = f"{self.jellyfin_url}/Items"
        params = {
            "includeItemTypes": "Playlist",
            "searchTerm": playlist_name,
            "recursive": "true",
        }

        playlist_id = None
        async with aiohttp.ClientSession() as session:
            # Poll Jellyfin until the playlist is detected by the library scan
            for attempt in range(15):  # 30 seconds total
                await asyncio.sleep(2)
                try:
                    async with session.get(search_url, params=params, headers=headers) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            items = data.get("Items", [])
                            # Find exact name match
                            for item in items:
                                if item.get("Name") == playlist_name:
                                    playlist_id = item.get("Id")
                                    break
                            if playlist_id:
                                logger.info(f"Found Jellyfin playlist ID: {playlist_id} on attempt {attempt + 1}")
                                break
                except Exception as e:
                    logger.warning(f"Jellyfin playlist search attempt {attempt + 1} failed: {e}")

            if not playlist_id:
                logger.warning(f"Jellyfin did not discover playlist '{playlist_name}' within 30 seconds.")
                return

            # Upload the primary image
            upload_url = f"{self.jellyfin_url}/Items/{playlist_id}/Images/Primary"
            upload_headers = {
                "X-Emby-Token": self.jellyfin_api_key,
                "Content-Type": mime_type,
            }
            try:
                async with session.post(upload_url, headers=upload_headers, data=cover_bytes) as resp:
                    if resp.status in (200, 204, 201):
                        logger.info(f"Successfully uploaded cover art to Jellyfin playlist '{playlist_name}'.")
                    else:
                        text = await resp.text()
                        logger.warning(f"Failed to upload Jellyfin playlist cover: HTTP {resp.status} - {text}")
            except Exception as e:
                logger.error(f"Error uploading cover art to Jellyfin: {e}")

    # ── Analysis Job Subscriptions (SSE) ────────────────────────────────────
    def subscribe_analysis(self, analysis_id: str) -> asyncio.Queue:
        """Subscribe to analysis job progress updates via SSE."""
        q: asyncio.Queue = asyncio.Queue()
        if analysis_id not in self._analysis_subscribers:
            self._analysis_subscribers[analysis_id] = set()
        self._analysis_subscribers[analysis_id].add(q)
        return q

    def unsubscribe_analysis(self, analysis_id: str, q: asyncio.Queue) -> None:
        """Unsubscribe from analysis job updates."""
        if analysis_id in self._analysis_subscribers:
            self._analysis_subscribers[analysis_id].discard(q)

    def _notify_analysis_subscribers(self, job: AnalysisJob) -> None:
        """Send analysis job status to all connected SSE subscribers."""
        subs = self._analysis_subscribers.get(job.analysis_id, set())
        status = job.to_dict()
        for q in subs:
            try:
                q.put_nowait(status)
            except asyncio.QueueFull:
                pass

    # ── Import Job Subscriptions (SSE) ───────────────────────────────────────
    def subscribe_import(self, import_id: str) -> asyncio.Queue:
        """Subscribe to import job updates."""
        q: asyncio.Queue = asyncio.Queue()
        if import_id not in self._import_subscribers:
            self._import_subscribers[import_id] = set()
        self._import_subscribers[import_id].add(q)
        return q

    def unsubscribe_import(self, import_id: str, q: asyncio.Queue) -> None:
        """Unsubscribe from import job updates."""
        if import_id in self._import_subscribers:
            self._import_subscribers[import_id].discard(q)

    def _notify_import_subscribers(self, job: ImportJob) -> None:
        """Send import job status to all subscribers."""
        subs = self._import_subscribers.get(job.import_id, set())
        status = job.to_dict()
        for q in subs:
            try:
                q.put_nowait(status)
            except asyncio.QueueFull:
                pass
