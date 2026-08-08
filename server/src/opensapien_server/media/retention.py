"""Tiered retention: audio expires, derived text is kept (spec §5.3, D8).

Audio is the bulky artifact and the most sensitive one — it is the raw
recording of a room, not a summary of it. The transcripts and memories derived
from it are the product. So they are kept and it is not.

**This asymmetry must be stated plainly in the app's privacy copy.** "The
recording expires but the transcript does not" is emphatically not what a user
assumes by default, and a retention setting that quietly means something
narrower than it sounds is worse than no setting at all.

``audio_days = 0`` disables the sweep rather than deleting everything
immediately — a zero in a retention field reads as "off" far more often than
it reads as "purge now", and the destructive reading is unrecoverable.
"""
from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 3600.0
_SECONDS_PER_DAY = 86_400.0


class RetentionSweeper:
    """Deletes audio for sessions untouched for longer than the retention window.

    Age is measured by the frame log's own mtime rather than by joining
    against the event store: the file is what is being deleted, its mtime is
    the last time audio was written to it, and that keeps the sweep correct
    even for a session whose events were already deleted.
    """

    def __init__(
        self,
        *,
        audio_store,
        settings_store,
        interval_s: float = DEFAULT_INTERVAL_S,
        now=time.time,
    ) -> None:
        self._audio = audio_store
        self._settings = settings_store
        self._interval = interval_s
        self._now = now
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def sweep_once(self) -> list[str]:
        """Delete expired audio. Returns the sessions swept."""
        days = self._settings.get().retention.audio_days
        if days <= 0:
            return []
        cutoff = self._now() - days * _SECONDS_PER_DAY
        swept: list[str] = []
        for session_id in self._audio.sessions():
            path: Path = self._audio.log_path(session_id)
            try:
                if not path.exists() or path.stat().st_mtime > cutoff:
                    continue
                self._audio.delete(session_id)
                self._delete_cached_segments(session_id)
                swept.append(session_id)
            except Exception:
                # One unreadable file must not stop the rest expiring — the
                # whole point is that this content does not linger.
                log.exception("retention_sweep_session_failed session=%s", session_id)
        if swept:
            log.info("retention_swept sessions=%d days=%d", len(swept), days)
        return swept

    def _delete_cached_segments(self, session_id: str) -> None:
        """Drop the muxed Ogg caches for a session whose source audio is gone.

        Skipping this would leave the cache serving a recording after its
        source was deliberately expired — the cache would become the thing
        retention failed to delete.
        """
        cache_dir = self._audio.log_path(session_id).parent / "seg"
        if not cache_dir.exists():
            return
        prefix = f"{self._audio._safe(session_id)}_"
        for path in cache_dir.glob(f"{prefix}*.ogg"):
            path.unlink(missing_ok=True)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="retention-sweeper")
        log.info("retention_sweeper_started interval_s=%s", self._interval)

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self._interval)
                return
            except asyncio.TimeoutError:
                pass
            try:
                # Filesystem work goes to a thread: a sweep over a large
                # audio directory would otherwise stall the event loop and
                # with it every live connection.
                await asyncio.to_thread(self.sweep_once)
            except Exception:
                log.exception("retention_sweep_failed")
