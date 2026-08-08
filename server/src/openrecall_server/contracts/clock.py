"""Canonical wall-clock seam for the server.

Every timestamp in the system is produced through a :class:`Clock`. Production
wires :class:`SystemClock`; tests inject :class:`FakeClock` so timestamps in
audit records, retrieval results, and cache keys are deterministic.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Single seam for "what time is it?" — the server never calls
    :func:`datetime.now` directly."""

    def now(self) -> datetime:
        """Return the current time as a tz-aware UTC ``datetime``."""
        ...


class SystemClock:
    """Production clock — delegates to :func:`datetime.now` with UTC."""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """Test clock — starts at a given time, advances in seconds.

    Default start is the Unix epoch (1970-01-01 UTC) so an uninitialised
    fake is clearly distinct from any wall-clock value.
    """

    def __init__(
        self, start: datetime | None = None
    ) -> None:
        self._now = start or datetime(1970, 1, 1, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Move the clock forward by ``seconds``."""
        from datetime import timedelta
        self._now = self._now + timedelta(seconds=seconds)

    def set_now(self, value: datetime) -> None:
        """Jump the clock to ``value`` directly."""
        self._now = value
