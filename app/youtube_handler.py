"""Handler for YouTube downloads via yt-dlp."""

import asyncio
import datetime
import logging
import uuid
import os
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncGenerator

import yt_dlp
from app.streamrip_handler import DownloadJob

logger = logging.getLogger(__name__)

class YoutubeHandler:
    def __init__(self, music_dir: str):
        self.music_dir = Path(music_dir)
        self.queue: asyncio.Queue[DownloadJob] = asyncio.Queue()
        self.active_job: DownloadJob | None = None
        self.completed_jobs: deque[DownloadJob] = deque(maxlen=50)
        self.queued_jobs: list[DownloadJob] = []

        self._worker_task: asyncio.Task | None = None
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def start_worker(self):
        """Start the background download worker."""
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._download_worker())

    async def stop_worker(self):
        """Stop the background download worker."""
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        """Subscribe to queue state changes."""
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any]]) -> None:
        """Unsubscribe from queue state changes."""
        self._subscribers.discard(q)

    def _notify_subscribers(self) -> None:
        """Broadcast current queue state to all subscribers."""
        if not self._subscribers:
            return
        state = self.get_queue_status()
        dead_queues = set()
        for q in self._subscribers:
            try:
                q.put_nowait(state)
            except Exception:
                dead_queues.add(q)
        self._subscribers -= dead_queues

    def get_queue_status(self) -> dict[str, Any]:
        """Return the current state of the queue."""
        active = self.active_job.to_dict() if self.active_job else None
        return {
            "active_job": active,
            "queued_jobs": [j.to_dict() for j in self.queued_jobs],
            "completed_jobs": [j.to_dict() for j in reversed(self.completed_jobs)],
        }

    from cachetools import TTLCache
    from asyncache import cached

    @cached(cache=TTLCache(maxsize=100, ttl=300))
    async def search(self, query: str, limit: int = 10, media_type: str = "track") -> list[dict[str, Any]]:
        """Search YouTube for tracks."""
        if not query.strip():
            return []
        
        ydl_opts = {
            'extract_flat': True,
            'quiet': True,

        }
        
        def _do_search():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                
        info = await asyncio.to_thread(_do_search)
        results = []
        for entry in info.get('entries', []):
            thumbnails = entry.get('thumbnails', [])
            cover_art_url = thumbnails[-1].get('url', '') if thumbnails else ''
            results.append({
                "type": "track",
                "id": entry.get("id", ""),
                "title": entry.get("title", ""),
                "artist": entry.get("uploader", ""),
                "album": "YouTube",
                "album_id": entry.get("channel_id", ""),
                "duration": entry.get("duration", 0),
                "year": "", 
                "cover_art_url": cover_art_url,
            })
        return results

    async def enqueue_download(
        self,
        url: str,
        title: str,
        artist: str,
        cover_art_url: str = "",
        quality: int | None = None
    ) -> DownloadJob:
        """Add a YouTube download to the queue."""
        job = DownloadJob(
            job_id=str(uuid.uuid4()),
            album_id=url, # using url as album_id
            title=title,
            artist=artist,
            status="queued",
            queued_at=datetime.datetime.now(),
            download_type="youtube",
            quality=quality,
            cover_art_url=cover_art_url,
        )
        self.queued_jobs.append(job)
        await self.queue.put(job)
        self._notify_subscribers()
        return job

    async def enqueue_playlist(self, url: str) -> DownloadJob:
        """Add a YouTube playlist download to the queue."""
        ydl_opts = {'extract_flat': True, 'quiet': True}
        def _get_info():
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                return ydl.extract_info(url, download=False)
        info = await asyncio.to_thread(_get_info)
        title = info.get("title", "YouTube Playlist")
        uploader = info.get("uploader", "Unknown Uploader")
        thumbnails = info.get("thumbnails", [])
        cover = thumbnails[-1].get("url", "") if thumbnails else ""
        
        return await self.enqueue_download(
            url=url,
            title=title,
            artist=uploader,
            cover_art_url=cover,
        )

    async def _download_worker(self) -> None:
        """Worker loop that pulls from the queue and downloads."""
        while True:
            job = await self.queue.get()
            self.active_job = job
            self.queued_jobs.remove(job)
            job.status = "downloading"
            self._notify_subscribers()

            try:
                # We'll use FLAC by default, or MP3 if quality is low? yt-dlp quality:
                ydl_opts = {
                    'format': 'bestaudio/best',
                    'postprocessors': [{
                        'key': 'FFmpegExtractAudio',
                        'preferredcodec': 'flac',
                    }, {
                        'key': 'FFmpegMetadata',
                        'add_metadata': True,
                    }, {
                        'key': 'EmbedThumbnail',
                    }],
                    'outtmpl': str(self.music_dir / "YouTube" / "%(uploader)s" / "%(title)s.%(ext)s"),
                    'writethumbnail': True,
                    'quiet': True,
                    'noprogress': True,
                }
                def _do_download():
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        ydl.download([job.album_id])

                await asyncio.to_thread(_do_download)
                job.status = "completed"
            except Exception as e:
                logger.error(f"YouTube download failed for {job.title}: {e}")
                job.status = "failed"
                job.error = str(e)
            finally:
                self.completed_jobs.append(job)
                self.active_job = None
                self._notify_subscribers()
                self.queue.task_done()
