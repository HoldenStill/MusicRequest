"""Handler for Streamrip operations and Qobuz API searching."""

import asyncio
import datetime
import json
import logging
import os
import shutil
import subprocess
import tomllib
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

import aiohttp

logger = logging.getLogger(__name__)


@dataclass
class DownloadJob:
    """Represents a download job."""
    job_id: str
    album_id: str
    title: str
    artist: str
    status: str
    queued_at: datetime.datetime
    error: str | None = None
    download_type: str = "album"      # "album" or "track"
    track_id: str | None = None       # Only used when download_type == "track"
    quality: int | None = None        # Quality override (1-4), None = use config default
    cover_art_url: str = ""           # For display in the queue widget

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary."""
        return {
            "job_id": self.job_id,
            "album_id": self.album_id,
            "title": self.title,
            "artist": self.artist,
            "status": self.status,
            "queued_at": self.queued_at.isoformat(),
            "error": self.error,
            "download_type": self.download_type,
            "track_id": self.track_id,
            "quality": self.quality,
            "cover_art_url": self.cover_art_url,
        }


class StreamripHandler:
    """Handles streamrip queue and Qobuz searching."""

    def __init__(self, config_path: str, music_dir: str):
        self.config_path = config_path
        self.music_dir = music_dir

        self.app_id = ""
        self.email_or_userid = ""
        self.password_or_token = ""
        self.use_auth_token = False

        self._user_auth_token: str | None = None
        self._load_config()

        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self.active_job: DownloadJob | None = None
        self.completed_jobs: deque[DownloadJob] = deque(maxlen=50)
        self.queued_jobs: list[DownloadJob] = []

        self._worker_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def _load_config(self) -> None:
        """Load Qobuz config from streamrip config file."""
        if not os.path.exists(self.config_path):
            logger.warning(f"Config file {self.config_path} not found.")
            return

        try:
            with open(self.config_path, "rb") as f:
                config = tomllib.load(f)
            qobuz_cfg = config.get("qobuz", {})
            self.app_id = qobuz_cfg.get("app_id", "")
            self.email_or_userid = qobuz_cfg.get("email_or_userid", "")
            self.password_or_token = qobuz_cfg.get("password_or_token", "")
            self.use_auth_token = qobuz_cfg.get("use_auth_token", False)

            if self.use_auth_token:
                self._user_auth_token = self.password_or_token
        except Exception as e:
            logger.error(f"Error loading streamrip config: {e}")

    async def _get_auth_token(self) -> str | None:
        """Get or refresh Qobuz auth token."""
        if self._user_auth_token:
            return self._user_auth_token

        if not self.app_id or not self.email_or_userid or not self.password_or_token:
            logger.error("Missing Qobuz credentials in config.")
            return None

        url = "https://www.qobuz.com/api.json/0.2/user/login"
        params = {
            "email": self.email_or_userid,
            "password": self.password_or_token,
        }
        headers = {"X-App-Id": self.app_id}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, params=params, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    self._user_auth_token = data.get("user_auth_token")
                    return self._user_auth_token
                else:
                    text = await resp.text()
                    logger.error(f"Qobuz login failed: {resp.status} - {text}")
                    return None

    # ── Helper: create an authenticated Streamrip QobuzClient session ────────
    async def _create_qobuz_client(self):
        """Create and log into a QobuzClient, returning (client, sr_config)."""
        from streamrip.config import Config as SRConfig, OutdatedConfigError
        from streamrip.client.qobuz import QobuzClient

        try:
            sr_config = SRConfig(self.config_path)
        except OutdatedConfigError:
            logger.info("Config file is outdated. Auto-updating streamrip config...")
            SRConfig.update_file(self.config_path)
            sr_config = SRConfig(self.config_path)

        client = QobuzClient(sr_config)
        await client.login()

        # Attempt to save any scraped app_id/secrets back to the config file
        try:
            sr_config.save_file()
        except Exception as save_err:
            logger.warning(f"Could not save updated config to disk: {save_err}")

        return client, sr_config

    @staticmethod
    def _extract_cover_art(image_data: dict | None) -> str:
        """Extract and upscale cover art URL from Qobuz image data."""
        if not image_data or not isinstance(image_data, dict):
            return ""
        cover_art_url = image_data.get("large") or image_data.get("small") or ""
        if not isinstance(cover_art_url, str):
            return ""
        if cover_art_url:
            cover_art_url = cover_art_url.replace("_230.jpg", "_600.jpg")
            cover_art_url = cover_art_url.replace("_50.jpg", "_600.jpg")
        return cover_art_url

    @staticmethod
    def _extract_year(item: dict) -> str:
        """Extract release year from a Qobuz item."""
        if not isinstance(item, dict):
            return ""
        release_date = item.get("released_at") or item.get("release_date_original") or ""
        release_date = str(release_date)
        return release_date[:4] if len(release_date) >= 4 else ""

    @staticmethod
    def _estimate_album_size(tracks: list[dict], quality: int = 3) -> int:
        """Estimate album size in bytes based on track durations and quality.
        
        Quality levels (from streamrip):
        1: 320kbps MP3 (~5-6 KB/s)
        2: 16/44.1 FLAC (~180 KB/s) 
        3: 24/96 FLAC (~350 KB/s)
        4: 24/192 FLAC (~700 KB/s)
        
        Returns size in bytes.
        """
        if not tracks:
            return 0
            
        # Average bitrates per quality level (KB/s)
        quality_bitrates = {
            1: 5.5,  # MP3 ~5.5 KB/s
            2: 180,  # FLAC ~180 KB/s
            3: 350,  # FLAC ~350 KB/s
            4: 700   # FLAC ~700 KB/s
        }
        
        bitrate = quality_bitrates.get(quality, 350)  # Default to quality 3
        total_seconds = sum(track.get("duration", 0) for track in tracks)
        
        # Convert seconds to bytes (bitrate in KB/s * 1024 bytes/KB * seconds)
        estimated_size_kb = bitrate * total_seconds
        return int(estimated_size_kb * 1024)  # Return bytes

    def _format_file_size(self, size_bytes: int) -> str:
        """Convert bytes to human readable file size string."""
        if size_bytes == 0:
            return "0 B"
            
        size_names = ["B", "KB", "MB", "GB", "TB"]
        i = 0
        while size_bytes >= 1024 and i < len(size_names) - 1:
            size_bytes /= 1024
            i += 1
            
        return f"{size_bytes:.1f} {size_names[i]}"

    # ── Search ───────────────────────────────────────────────────────────────
    from cachetools import TTLCache
    from asyncache import cached

    @cached(cache=TTLCache(maxsize=100, ttl=300))
    async def search(self, query: str, limit: int = 10, media_type: str = "album") -> list[dict[str, Any]]:
        """Search Qobuz for albums, tracks, or artists using Streamrip's internal client."""
        client = None
        try:
            client, sr_config = await self._create_qobuz_client()

            url = f"https://www.qobuz.com/api.json/0.2/{media_type}/search"
            params = {"query": query, "limit": limit}

            async with client.session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Qobuz search failed: {resp.status} - {text}")
                    raise Exception(f"Search failed with status {resp.status}: {text[:100]}...")
                data = await resp.json()
        except Exception as e:
            logger.exception(f"Error during Qobuz search via Streamrip client: {e}")
            raise Exception(f"Failed to perform search: {str(e)[:100]}...")
        finally:
            if client and client.session:
                await client.session.close()

        if media_type == "track":
            return self._parse_track_results(data)
        elif media_type == "artist":
            return self._parse_artist_results(data)
        else:
            return self._parse_album_results(data)

    def _parse_album_results(self, data: dict) -> list[dict[str, Any]]:
        """Parse album search results from Qobuz API response."""
        results = []
        if not isinstance(data, dict):
            return results
        albums_obj = data.get("albums")
        if not isinstance(albums_obj, dict):
            return results
        albums = albums_obj.get("items")
        if not isinstance(albums, list):
            return results
        for item in albums:
            if not isinstance(item, dict):
                continue
            album_id = str(item.get("id") or "")
            if not album_id:
                continue
            artist_data = item.get("artist")
            if not isinstance(artist_data, dict):
                artist_data = {}
            results.append({
                "type": "album",
                "album_id": album_id,
                "title": item.get("title") or "Unknown",
                "artist": artist_data.get("name") or "Unknown",
                "release_year": self._extract_year(item),
                "cover_art_url": self._extract_cover_art(item.get("image") if isinstance(item.get("image"), dict) else None),
                "tracks_count": item.get("tracks_count") or 0,
                "source": "qobuz",
                "estimated_size": 0,  # Will be populated when needed
                "estimated_size_readable": "Unknown"
            })
        return results

    def _parse_track_results(self, data: dict) -> list[dict[str, Any]]:
        """Parse track search results from Qobuz API response."""
        results = []
        if not isinstance(data, dict):
            return results
        tracks_obj = data.get("tracks")
        if not isinstance(tracks_obj, dict):
            return results
        tracks = tracks_obj.get("items")
        if not isinstance(tracks, list):
            return results
        for item in tracks:
            if not isinstance(item, dict):
                continue
            track_id = str(item.get("id") or "")
            if not track_id:
                continue
            album_data = item.get("album")
            if not isinstance(album_data, dict):
                album_data = {}
            performer = item.get("performer")
            if not isinstance(performer, dict):
                performer = {}
            artist_dict = item.get("artist")
            if not isinstance(artist_dict, dict):
                artist_dict = {}
            results.append({
                "type": "track",
                "track_id": track_id,
                "title": item.get("title") or "Unknown",
                "artist": performer.get("name") or artist_dict.get("name") or "Unknown",
                "duration": item.get("duration") or 0,
                "album_title": album_data.get("title") or "",
                "album_id": str(album_data.get("id") or ""),
                "cover_art_url": self._extract_cover_art(album_data.get("image") if isinstance(album_data.get("image"), dict) else None),
                "source": "qobuz",
            })
        return results

    def _parse_artist_results(self, data: dict) -> list[dict[str, Any]]:
        """Parse artist search results from Qobuz API response."""
        results = []
        if not isinstance(data, dict):
            return results
        artists_obj = data.get("artists")
        if not isinstance(artists_obj, dict):
            return results
        artists = artists_obj.get("items")
        if not isinstance(artists, list):
            return results
        for item in artists:
            if not isinstance(item, dict):
                continue
            artist_id = str(item.get("id") or "")
            if not artist_id:
                continue
            image_data = item.get("image")
            if not isinstance(image_data, dict):
                image_data = {}
            image_url = image_data.get("large") or image_data.get("small") or item.get("picture") or ""
            if not isinstance(image_url, str):
                image_url = ""
            albums_count = item.get("albums_count")
            if albums_count is None:
                albums_count = 0
            results.append({
                "type": "artist",
                "artist_id": artist_id,
                "name": item.get("name") or "Unknown",
                "image_url": image_url,
                "albums_count": albums_count,
                "source": "qobuz",
            })
        return results

    # ── Album Tracklist ──────────────────────────────────────────────────────
    async def get_album_tracks(self, album_id: str) -> dict[str, Any] | None:
        """Fetch the full tracklist for an album."""
        client = None
        try:
            client, sr_config = await self._create_qobuz_client()

            url = "https://www.qobuz.com/api.json/0.2/album/get"
            params = {"album_id": album_id}

            async with client.session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Album fetch failed: {resp.status} - {text}")
                    raise Exception(f"Album fetch failed with status {resp.status}: {text[:100]}...")
                data = await resp.json()
        except Exception as e:
            logger.exception(f"Error fetching album tracks for {album_id}: {e}")
            raise Exception(f"Failed to fetch album details: {str(e)[:100]}...")
        finally:
            if client and client.session:
                await client.session.close()

        if not isinstance(data, dict):
            return None

        tracks = []
        tracks_obj = data.get("tracks")
        tracks_data = tracks_obj.get("items") if isinstance(tracks_obj, dict) else []
        if not isinstance(tracks_data, list):
            tracks_data = []

        artist_obj = data.get("artist")
        if not isinstance(artist_obj, dict):
            artist_obj = {}
        default_artist = artist_obj.get("name") or "Unknown"

        for t in tracks_data:
            if not isinstance(t, dict):
                continue
            performer = t.get("performer")
            if not isinstance(performer, dict):
                performer = {}
            tracks.append({
                "track_id": str(t.get("id") or ""),
                "track_number": t.get("track_number") or 0,
                "title": t.get("title") or "Unknown",
                "artist": performer.get("name") or default_artist,
                "duration": t.get("duration") or 0,
                "bit_depth": t.get("maximum_bit_depth"),
                "sample_rate": t.get("maximum_sampling_rate"),
            })

        image_obj = data.get("image")
        cover_art = self._extract_cover_art(image_obj if isinstance(image_obj, dict) else None)

        # Calculate estimated file size for the album (default quality 3)
        estimated_size = self._estimate_album_size(tracks, quality=3)
        
        return {
            "album_id": str(data.get("id") or album_id),
            "title": data.get("title") or "Unknown",
            "artist": default_artist,
            "cover_art_url": cover_art,
            "release_year": self._extract_year(data),
            "release_date": data.get("release_date_original") or data.get("release_date") or "",
            "tracks_count": data.get("tracks_count") or len(tracks),
            "tracks": tracks,
            "estimated_size": estimated_size,
            "estimated_size_readable": self._format_file_size(estimated_size)
        }

    # ── Artist Details ───────────────────────────────────────────────────────
    async def get_artist_details(self, artist_id: str) -> dict[str, Any] | None:
        """Fetch artist details, discography and top tracks, with graceful fallbacks."""
        client = None
        base_artist_data = None
        albums_data = None
        top_tracks_list = []
        albums = []

        try:
            client, sr_config = await self._create_qobuz_client()

            # 1. Fetch base artist profile first
            artist_url = "https://www.qobuz.com/api.json/0.2/artist/get"
            base_params = {"artist_id": artist_id}

            async with client.session.get(artist_url, params=base_params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    logger.error(f"Base artist fetch failed: {resp.status} - {text}")
                    return None
                base_artist_data = await resp.json()

            if not isinstance(base_artist_data, dict):
                return None

            # 2. Fetch albums (discography) in a safe sub-try block
            try:
                albums_params = {"artist_id": artist_id, "extra": "albums", "limit": 50}
                async with client.session.get(artist_url, params=albums_params) as resp:
                    if resp.status == 200:
                        albums_data = await resp.json()
                    else:
                        logger.warning(f"Artist albums fetch returned non-200: {resp.status}")
            except Exception as e:
                logger.warning(f"Could not fetch albums for artist {artist_id}: {e}")

            # 3. Fetch top tracks in a safe sub-try block
            try:
                top_tracks_params = {"artist_id": artist_id, "extra": "tracks_top", "limit": 5}
                async with client.session.get(artist_url, params=top_tracks_params) as resp:
                    if resp.status == 200:
                        top_data = await resp.json()
                        if isinstance(top_data, dict):
                            tracks_top = top_data.get("tracks_top")
                            if not isinstance(tracks_top, (dict, list)):
                                tracks_top = top_data.get("tracks")

                            items = None
                            if isinstance(tracks_top, list):
                                items = tracks_top
                            elif isinstance(tracks_top, dict):
                                items = tracks_top.get("items")

                            if isinstance(items, list):
                                for t in items:
                                    if not isinstance(t, dict):
                                        continue
                                    album_data = t.get("album")
                                    if not isinstance(album_data, dict):
                                        album_data = {}
                                    top_tracks_list.append({
                                        "track_id": str(t.get("id") or ""),
                                        "title": t.get("title") or "Unknown",
                                        "duration": t.get("duration") or 0,
                                        "album_title": album_data.get("title") or "",
                                        "album_id": str(album_data.get("id") or ""),
                                        "cover_art_url": self._extract_cover_art(
                                            album_data.get("image") if isinstance(album_data.get("image"), dict) else None
                                        ),
                                    })
            except Exception as e:
                logger.warning(f"Could not fetch top tracks for artist {artist_id}: {e}")

        except Exception as e:
            logger.exception(f"Error fetching artist details for {artist_id}")
            return None
        finally:
            if client and client.session:
                await client.session.close()

        # Parse albums from albums_data if available
        if isinstance(albums_data, dict):
            albums_obj = albums_data.get("albums")
            if isinstance(albums_obj, list):
                album_items = albums_obj
            elif isinstance(albums_obj, dict):
                album_items = albums_obj.get("items") if isinstance(albums_obj.get("items"), list) else []
            else:
                album_items = []

            for item in album_items:
                if not isinstance(item, dict):
                    continue
                albums.append({
                    "album_id": str(item.get("id") or ""),
                    "title": item.get("title") or "Unknown",
                    "release_year": self._extract_year(item),
                    "cover_art_url": self._extract_cover_art(
                        item.get("image") if isinstance(item.get("image"), dict) else None
                    ),
                    "tracks_count": item.get("tracks_count") or 0,
                })

        # Parse artist image from base_artist_data
        image_data = base_artist_data.get("image")
        if not isinstance(image_data, dict):
            image_data = {}
        image_url = image_data.get("large") or image_data.get("small") or base_artist_data.get("picture") or ""
        if not isinstance(image_url, str):
            image_url = ""

        # Parse biography from base_artist_data
        bio = base_artist_data.get("biography")
        biography = ""
        if isinstance(bio, dict):
            content = bio.get("content") or bio.get("summary") or ""
            if isinstance(content, str):
                biography = content
        elif isinstance(bio, str):
            biography = bio

        albums_count = base_artist_data.get("albums_count")
        if albums_count is None:
            albums_count = len(albums)

        return {
            "artist_id": str(base_artist_data.get("id") or artist_id),
            "name": base_artist_data.get("name") or "Unknown",
            "image_url": image_url,
            "biography": biography,
            "albums_count": albums_count,
            "albums": albums,
            "top_tracks": top_tracks_list[:5],
        }

    # ── Download Queue ───────────────────────────────────────────────────────
    async def enqueue_download(
        self,
        album_id: str,
        title: str,
        artist: str,
        download_type: str = "album",
        track_id: str | None = None,
        quality: int | None = None,
        cover_art_url: str = "",
    ) -> DownloadJob:
        """Enqueue an album or track for download."""
        job = DownloadJob(
            job_id=str(uuid.uuid4()),
            album_id=album_id,
            title=title,
            artist=artist,
            status="queued",
            queued_at=datetime.datetime.now(datetime.timezone.utc),
            download_type=download_type,
            track_id=track_id,
            quality=quality,
            cover_art_url=cover_art_url,
        )
        self.queued_jobs.append(job)
        await self.queue.put(job)
        self._notify_subscribers()
        return job

    def get_queue_status(self) -> dict[str, Any]:
        """Get the current state of the download queue."""
        return {
            "active": self.active_job.to_dict() if self.active_job else None,
            "queued": [job.to_dict() for job in self.queued_jobs],
            "completed": [job.to_dict() for job in self.completed_jobs],
        }

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to queue updates."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Unsubscribe from queue updates."""
        self._subscribers.discard(q)

    def _notify_subscribers(self) -> None:
        """Send the current status to all subscribers."""
        if not self._subscribers:
            return
        status = self.get_queue_status()
        for q in self._subscribers:
            try:
                q.put_nowait(status)
            except asyncio.QueueFull:
                pass

    async def _download_worker(self) -> None:
        """Worker task to process downloads."""
        while True:
            try:
                job = await self.queue.get()
                self.queued_jobs.remove(job)

                job.status = "downloading"
                self.active_job = job
                self._notify_subscribers()

                # Build the rip command
                rip_path = shutil.which("rip") or "rip"
                cmd = [
                    rip_path,
                    "--config-path", self.config_path,
                    "--folder", self.music_dir,
                    "--no-db",
                ]

                # Add quality override if specified
                if job.quality is not None:
                    cmd.extend(["--quality", str(job.quality)])

                # Determine the download URL based on type
                if job.download_type == "track" and job.track_id:
                    download_url = f"https://open.qobuz.com/track/{job.track_id}"
                else:
                    download_url = f"https://open.qobuz.com/album/{job.album_id}"

                cmd.extend(["url", download_url])

                try:
                    process = await asyncio.to_thread(
                        subprocess.run,
                        cmd,
                        capture_output=True,
                        text=True
                    )

                    if process.returncode == 0 and "ERROR" not in process.stdout and "ERROR" not in process.stderr:
                        job.status = "completed"
                        logger.info(f"Download completed successfully: {job.title}")
                    else:
                        job.status = "failed"
                        # Improve error messages
                        stderr_output = process.stderr.strip()
                        stdout_output = process.stdout.strip()
                        
                        error_msg = ""
                        for line in stdout_output.split("\n") + stderr_output.split("\n"):
                            if "ERROR" in line or "Exception" in line:
                                error_msg = line.strip()
                                break
                        
                        if error_msg:
                            job.error = f"Download failed: {error_msg[:200]}"
                        elif stderr_output:
                            job.error = f"Download failed: {stderr_output[:200]}..."
                        elif stdout_output:
                            job.error = f"Download failed: {stdout_output[:200]}..."
                        else:
                            job.error = "Unknown download error"
                        logger.error(f"Download failed for {job.title}: {job.error}")
                except Exception as e:
                    job.status = "failed"
                    job.error = f"Download process error: {str(e)[:100]}..."
                    logger.exception(f"Download worker exception for {job.title}")

                self.completed_jobs.append(job)
                self.active_job = None
                self._notify_subscribers()
                self.queue.task_done()
            except Exception as e:
                # Log unexpected errors but continue processing
                logger.exception(f"Unexpected error in download worker: {e}")
                try:
                    if job and job.status != "failed":
                        job.status = "failed"
                        job.error = f"Unexpected worker error: {str(e)[:100]}..."
                        if self.active_job == job:
                            self.active_job = None
                        self._notify_subscribers()
                except Exception:
                    pass  # Ignore errors in error handling
                self.queue.task_done()

    async def start_worker(self) -> None:
        """Start the background worker."""
        if self._worker_task is None:
            self._worker_task = asyncio.create_task(self._download_worker())

    async def stop_worker(self) -> None:
        """Stop the background worker."""
        if self._worker_task:
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
            self._worker_task = None
