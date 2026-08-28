"""Playlist Importer & Migration Tool for MusicRequest.

Supports scraping public Spotify playlists (zero-setup) and Qobuz playlists,
checking for local duplicates, downloading missing tracks, and generating
.m3u8 playlist files with cover art.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
import os
import re
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
    r"(?:https?://)?(?:open\.)?spotify\.com/(?:embed/)?playlist/([a-zA-Z0-9]+)"
)
QOBUZ_PLAYLIST_RE = re.compile(
    r"(?:https?://)?(?:open|play|www)\.qobuz\.com/.*playlist/([a-zA-Z0-9-]+)"
)

# ── Normalization helpers ────────────────────────────────────────────────────
_BRACKET_RE = re.compile(r"\s*[\(\[\{].*?[\)\]\}]\s*")
_FEAT_RE = re.compile(r"\s*(?:feat\.?|ft\.?|featuring|with)\s+.*", re.IGNORECASE)
_NON_ALNUM_RE = re.compile(r"[^a-z0-9 ]")
_MULTI_SPACE_RE = re.compile(r"\s+")
_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|&|/|;|\\|\bx\b|\bvs\.?\b|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bwith\b)\s*",
    re.IGNORECASE,
)
_TITLE_SUFFIX_RE = re.compile(
    r"\s*-\s*(?:remaster(?:ed)?(?:\s*\d+)?|radio edit|explicit(?: version)?|single version|album version|live(?: at .*)?|acoustic|bonus track|from\s+.*|theme from\s+.*|motion picture\s+.*|soundtrack\s+.*|original mix|extended mix|club mix|with\s+.*|feat\.?\s+.*|ft\.?\s+.*).*$",
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

    # Split by delimiters (commas, &, feat, with, etc.)
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

    return [v for v in variants if v]


def _get_title_variants(title_str: str) -> list[str]:
    """Generate title variations (full, without brackets, without suffix, etc.)."""
    if not title_str or not isinstance(title_str, str):
        return []

    variants: set[str] = set()
    raw_norm = _normalize(title_str)
    if raw_norm:
        variants.add(raw_norm)

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

        # Active import jobs
        self.import_jobs: dict[str, ImportJob] = {}
        self._import_subscribers: dict[str, set[asyncio.Queue]] = {}

    # ── URL Parsing ──────────────────────────────────────────────────────────
    @staticmethod
    def parse_playlist_url(url: str) -> tuple[str, str]:
        """Parse a playlist URL and return (source, playlist_id).

        Raises ValueError if the URL is not recognized.
        """
        url = url.strip()
        m = SPOTIFY_PLAYLIST_RE.search(url)
        if m:
            return "spotify", m.group(1)
        m = QOBUZ_PLAYLIST_RE.search(url)
        if m:
            return "qobuz", m.group(1)
        raise ValueError(
            "Unrecognized playlist URL. Please paste a Spotify or Qobuz playlist link."
        )

    # ── Spotify Scraping ─────────────────────────────────────────────────────
    async def _fetch_spotify_playlist(self, playlist_id: str) -> PlaylistInfo:
        """Scrape a Spotify embed page to extract playlist metadata + tracks."""
        url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0.0.0 Safari/537.36"
            ),
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
            r'<script\s+id="__NEXT_DATA__"[^>]*>(.*?)</script>',
            html, re.DOTALL,
        )
        if not match:
            raise RuntimeError(
                "Could not find __NEXT_DATA__ in Spotify embed page. "
                "The playlist may be private or the page structure changed."
            )

        data = json.loads(match.group(1))

        # Navigate the JSON tree
        entity = (
            data.get("props", {})
                .get("pageProps", {})
                .get("state", {})
                .get("data", {})
                .get("entity", {})
        )

        if not entity:
            raise RuntimeError("Could not extract playlist entity from Spotify data.")

        playlist_name = entity.get("name", "Untitled Playlist")
        description = entity.get("description", "")
        # Owner may be nested or absent
        owner = entity.get("subtitle", "")

        # Cover art — try the images array first
        cover_art_url = ""
        images = entity.get("images", [])
        if images:
            # Try to get the largest image
            best = max(images, key=lambda img: img.get("width", 0) or 0)
            cover_art_url = best.get("url", "")
        if not cover_art_url:
            # Fallback: coverArt or visualIdentity
            cover_art = entity.get("coverArt", entity.get("visualIdentity", {}))
            if isinstance(cover_art, dict):
                sources = cover_art.get("sources", [])
                if sources:
                    best = max(sources, key=lambda s: s.get("width", 0) or 0)
                    cover_art_url = best.get("url", "")

        # Parse tracks
        tracks: list[PlaylistTrack] = []
        raw_tracks = entity.get("trackList", [])
        for item in raw_tracks:
            title = item.get("title", "")
            artist = item.get("subtitle", "")
            if not title:
                continue

            duration_ms = item.get("duration", 0) or 0

            # Track cover art
            track_cover = ""
            cover_art_data = item.get("coverArt", {})
            if isinstance(cover_art_data, dict):
                sources = cover_art_data.get("sources", [])
                if sources:
                    track_cover = sources[0].get("url", "")

            tracks.append(PlaylistTrack(
                title=title,
                artist=artist,
                duration_ms=duration_ms,
                cover_art_url=track_cover or cover_art_url,
            ))

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

    # ── Library Indexing (On-Demand, Mutagen) ────────────────────────────────
    async def _build_library_index(self) -> dict[str, Any]:
        """Scan the music directory for audio files, read tags with mutagen,
        and return a structured index with direct keys and title fallback mapping.

        This runs in a thread to avoid blocking the event loop.
        The index is built on-demand and discarded after use.
        """
        return await asyncio.to_thread(self._scan_library_sync)

    def _scan_library_sync(self) -> dict[str, Any]:
        """Synchronous library scanner using mutagen."""
        import mutagen

        keys_index: dict[str, str] = {}
        titles_index: dict[str, list[tuple[str, str]]] = {}
        music_path = Path(self.music_dir)
        if not music_path.exists():
            logger.warning(f"Music directory does not exist: {self.music_dir}")
            return {"keys": keys_index, "titles": titles_index}

        audio_exts = {".flac", ".mp3", ".ogg", ".m4a", ".opus", ".wma", ".wav", ".aac"}
        scanned = 0
        errors = 0

        for filepath in music_path.rglob("*"):
            if filepath.suffix.lower() not in audio_exts:
                continue
            try:
                tags = mutagen.File(str(filepath), easy=True)
                if tags is None:
                    continue

                # Extract all artists and titles from tags
                titles: list[str] = []
                artists: list[str] = []
                if hasattr(tags, "get"):
                    raw_titles = tags.get("title", [])
                    titles = [t for t in raw_titles if isinstance(t, str) and t]
                    raw_artists = tags.get("artist", [])
                    album_artists = tags.get("albumartist", [])
                    for a in list(raw_artists) + list(album_artists):
                        if isinstance(a, str) and a and a not in artists:
                            artists.append(a)

                if not titles or not artists:
                    continue

                path_str = str(filepath)
                for t in titles:
                    for a in artists:
                        # Generate all key permutations
                        for k in _make_lookup_keys(a, t):
                            keys_index[k] = path_str
                        # Populate title index for fuzzy fallback
                        for t_var in _get_title_variants(t):
                            norm_var = _normalize(t_var)
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
        return {"keys": keys_index, "titles": titles_index}

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
            for item in data.get("Items", []):
                title = item.get("Name", "")
                artists = item.get("Artists", [])
                album_artist = item.get("AlbumArtist", "")
                path = item.get("Path", "")

                all_artists: list[str] = [a for a in artists if isinstance(a, str) and a]
                if album_artist and album_artist not in all_artists:
                    all_artists.append(album_artist)

                if title and all_artists and path:
                    for a in all_artists:
                        for k in _make_lookup_keys(a, title):
                            keys_index[k] = path
                    for t_var in _get_title_variants(title):
                        norm_var = _normalize(t_var)
                        if norm_var not in titles_index:
                            titles_index[norm_var] = []
                        titles_index[norm_var].append((all_artists[0], path))

            logger.info(f"Jellyfin index built: {len(keys_index)} lookup keys")
            return {"keys": keys_index, "titles": titles_index}
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

        # 1. Try all lookup keys from original artist/title
        keys_to_try = _make_lookup_keys(track.artist, track.title)

        # 2. Try lookup keys from matched Qobuz artist/title if available
        if track.matched_artist or track.matched_title:
            m_artist = track.matched_artist or track.artist
            m_title = track.matched_title or track.title
            for k in _make_lookup_keys(m_artist, m_title):
                if k not in keys_to_try:
                    keys_to_try.append(k)
            for k in _make_lookup_keys(track.artist, m_title):
                if k not in keys_to_try:
                    keys_to_try.append(k)
            for k in _make_lookup_keys(m_artist, track.title):
                if k not in keys_to_try:
                    keys_to_try.append(k)

        for k in keys_to_try:
            if k in direct_keys:
                return direct_keys[k]

        # 3. Fuzzy title match fallback
        if title_index:
            title_candidates = _get_title_variants(track.title)
            if track.matched_title:
                title_candidates.extend(_get_title_variants(track.matched_title))

            artist_variants = _extract_artist_variants(track.artist)
            if track.matched_artist:
                artist_variants.extend(_extract_artist_variants(track.matched_artist))

            for t_var in title_candidates:
                norm_t = _normalize(t_var)
                candidates = title_index.get(norm_t, [])
                for cand_artist, cand_path in candidates:
                    cand_artist_norm = _normalize(cand_artist)
                    for a_var in artist_variants:
                        if a_var == cand_artist_norm or a_var in cand_artist_norm or cand_artist_norm in a_var:
                            return cand_path

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
    async def _match_track_on_qobuz(self, track: PlaylistTrack) -> bool:
        """Search Qobuz for a matching track and populate qobuz_track_id.

        Returns True if a match was found.
        """
        query = f"{track.artist} {track.title}"
        try:
            results = await self.handler.search(query, limit=5, media_type="track")
        except Exception as e:
            logger.warning(f"Qobuz search failed for '{query}': {e}")
            return False

        if not results:
            return False

        # Score each result
        target_duration_s = track.duration_ms / 1000
        best_score = 0.0
        best_result = None

        for result in results:
            r_title = result.get("title", "")
            r_artist = result.get("artist", "")
            r_duration = result.get("duration", 0)

            # Title similarity
            title_match = 1.0 if _normalize(r_title) == _normalize(track.title) else 0.0
            if not title_match:
                # Partial match: check if one contains the other
                nt = _normalize(track.title)
                nr = _normalize(r_title)
                if nt in nr or nr in nt:
                    title_match = 0.7

            # Artist similarity
            artist_match = 1.0 if _normalize(r_artist) == _normalize(track.artist) else 0.0
            if not artist_match:
                na = _normalize(track.artist)
                nra = _normalize(r_artist)
                if na in nra or nra in na:
                    artist_match = 0.7

            # Duration similarity (within 4 seconds tolerance)
            duration_diff = abs(r_duration - target_duration_s) if target_duration_s > 0 else 0
            duration_match = 1.0 if duration_diff <= 4 else max(0, 1.0 - duration_diff / 30)

            score = (title_match * 0.45) + (artist_match * 0.35) + (duration_match * 0.20)

            if score > best_score:
                best_score = score
                best_result = result

        # Require minimum confidence
        if best_result and best_score >= 0.5:
            track.qobuz_track_id = best_result.get("track_id", "")
            track.qobuz_album_id = best_result.get("album_id", "")
            track.matched_title = best_result.get("title", "")
            track.matched_artist = best_result.get("artist", "")
            track.match_confidence = round(best_score, 2)
            return True

        return False

    # ── Analyze Playlist (Public API) ────────────────────────────────────────
    async def analyze_playlist(self, url: str) -> PlaylistInfo:
        """Full analysis pipeline: parse URL → fetch tracks → check duplicates.

        The library index is built on-demand and garbage-collected after use.
        """
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

        # Match missing tracks on Qobuz (with rate limiting)
        missing = [t for t in playlist.tracks if not t.is_local]
        for i, track in enumerate(missing):
            if source == "qobuz" and track.qobuz_track_id:
                track.match_confidence = 1.0
                continue
            await self._match_track_on_qobuz(track)
            # Small delay to avoid hammering the API
            if i < len(missing) - 1:
                await asyncio.sleep(0.15)

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

            # Update track local paths with smart multi-alias resolution
            for track in playlist.tracks:
                path = self._resolve_track_path(track, library_index)
                if path:
                    track.is_local = True
                    track.local_path = path

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

        for track in playlist.tracks:
            if not track.is_local or not track.local_path:
                continue

            # Calculate relative path from playlist dir to the audio file
            try:
                audio_path = Path(track.local_path)
                rel_path = os.path.relpath(audio_path, playlist_dir)
                # Use forward slashes for cross-platform compatibility
                rel_path = rel_path.replace("\\", "/")
            except ValueError:
                # On Windows, relpath can fail across drives
                rel_path = track.local_path.replace("\\", "/")

            duration_s = track.duration_ms // 1000 if track.duration_ms else -1
            lines.append(f"#EXTINF:{duration_s},{track.artist} - {track.title}")
            lines.append(rel_path)

        m3u_content = "\n".join(lines) + "\n"
        m3u_path.write_text(m3u_content, encoding="utf-8")
        logger.info(f"Generated playlist file: {m3u_path}")

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
