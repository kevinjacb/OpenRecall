"""§F capture event — the durable, immutable unit of what the wearable captured.

Events are append-only and event-sourced: they are the source of truth from which
memory atoms (§G) are later derived. This first kind is ``transcript`` (a
transcribed audio window); the ``kind`` tag leaves room for vision/photo events to
join the same per-session timeline later without changing the store.

``start_ms`` is the offset of this event within the session's audio timeline, so
events place themselves in time independent of wall-clock ``created_at``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class CaptureEvent(BaseModel):
    model_config = ConfigDict(frozen=True)  # immutable once created

    event_id: str  # idempotency key; stable across at-least-once redelivery
    session_id: str
    seq: int  # per-session monotonic order
    kind: Literal["transcript"]
    created_at: datetime
    text: str
    duration_ms: int
    start_ms: int
    speaker: str | None = None  # assigned speaker UUID, or None (silence/no-speech hop)
    speaker_confidence: float | None = None  # cosine of the match, 0..1
    speaker_assignment: str | None = None  # "confirmed" | "tentative" | "none"
