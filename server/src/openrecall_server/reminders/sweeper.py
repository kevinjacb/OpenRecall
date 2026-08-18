"""Periodic sweep that fires due reminders through the proactive outbox.

Mirrors :class:`SegmentSweeper` / :class:`RetentionSweeper`: a tick-driven
task (reminder fire is clock-decided, not event-decided). On each tick,
``ReminderStore.due(now)`` returns the pending reminders whose ``due_at``
has passed; for each, enqueue a :class:`ProactiveMessage` on the
process-wide :class:`ProactiveOutbox` and ``mark_fired``. Idempotent —
``due()`` only returns ``status="pending"``, so a fired reminder is never
re-fired.

Delivered only while the relay is connected in v1 (the outbox drains to
the active GatewayCore); FCM push for background delivery is v1.1.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from ..gateway.core import ProactiveOutbox
from ..protocol.messages import ProactiveMessage
from .store import ReminderStore

log = logging.getLogger(__name__)

DEFAULT_INTERVAL_S = 30.0


class ReminderSweeper:
    """Fires due reminders on a tick. Best-effort; never re-raises."""

    def __init__(
        self,
        *,
        store: ReminderStore,
        outbox: ProactiveOutbox,
        clock=None,
        interval_s: float = DEFAULT_INTERVAL_S,
    ) -> None:
        self._store = store
        self._outbox = outbox
        self._clock = clock
        self._interval = interval_s
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def sweep_once(self, now: datetime | None = None) -> int:
        """Fire what is due. Returns the number of reminders fired.

        Synchronous and directly callable, so the behaviour can be tested
        without a running loop or a sleeping task.
        """
        if now is None:
            now = self._clock.now() if self._clock is not None else datetime.now(tz=timezone.utc)
        due = self._store.due(now)
        for r in due:
            self._outbox.enqueue(ProactiveMessage(
                type="proactive",
                session_id=r.session_id,
                request_id=f"reminder-{r.atom_id}",
                text=f"⏰ Reminder: {r.text}",
                atoms=(),
            ))
            self._store.mark_fired(r.atom_id, fired_at=now)
            log.info("reminder_fired atom=%s session=%s due_at=%s", r.atom_id, r.session_id, r.due_at)
        return len(due)

    async def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="reminder-sweeper")
        log.info("reminder_sweeper_started interval_s=%s", self._interval)

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
                return  # stop() was called
            except asyncio.TimeoutError:
                pass
            try:
                await asyncio.to_thread(self.sweep_once)
            except Exception:
                log.exception("reminder_sweep_failed")