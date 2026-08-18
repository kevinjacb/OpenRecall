"""§D device command model.

Commands are issued by the server and executed by the wearable. The Android relay
that carries them is untrusted, so each command is signed (see
:mod:`openrecall_server.commands.signing`) over its *canonical bytes*: a compact,
key-sorted JSON encoding that the server and the device compute identically.
Determinism is essential — any divergence would make a valid command fail
verification on the device.

Commands carry an ``expires_at`` so a delayed/replayed command is ignored once
stale, and a unique ``command_id`` so the device executes each at most once.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

CommandType = Literal[
    "capture_photo",
    "record_video",
    "start_audio",
    "stop_audio",
    "play_audio",
    "display_text",
    "show_status",
    "request_buffer",
    # P1 instruction processor: prospective audio + voice-activated video.
    "record_audio",
    "start_video",
    "stop_video",
    "flush_snapshots",
]


class Command(BaseModel):
    model_config = ConfigDict(frozen=True)

    command_id: str  # unique; device executes each command_id at most once
    session_id: str
    type: CommandType
    params: dict[str, Any] = {}
    issued_at: datetime
    expires_at: datetime
    # The user-facing dedup key. Two issues with the same key produce
    # the same command_id (P2-commands Phase 4). Optional for back-compat
    # with Phase 1 tests that don't care; the dispatcher's idempotency
    # falls back to command_id dedup when this is missing.
    idempotency_key: str | None = None

    def canonical_bytes(self) -> bytes:
        """Deterministic bytes to sign/verify: compact, fully key-sorted JSON."""
        return json.dumps(
            self.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expires_at
