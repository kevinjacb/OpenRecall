"""Command-event memory: one ``command_event`` atom per device-acked command.

Written from :meth:`GatewayCore._on_command_ack` (the device-ack hook), not at
dispatch time — issuing ≠ executing. ``atom_id = command_id`` gives natural
idempotency (one memory per command, even under retry/reissue). ``kind`` is
``command_event`` and ``source_pipeline_version`` is ``command`` so these are
distinct from ambient transcript-window memories.
"""
from __future__ import annotations

import logging

from ..memory.atom import MemoryAtom

logger = logging.getLogger(__name__)

_COMMAND_TEXT = {
    "capture_photo": "Took a photo",
    "record_video": "Recorded a video",
    "start_video": "Started a video",
    "stop_video": "Stopped the video",
    "start_audio": "Started audio recording",
    "stop_audio": "Stopped audio recording",
    "flush_snapshots": "Flushed snapshots",
}


class CommandMemoryWriter:
    def __init__(self, *, store, clock, id_generator=None) -> None:
        self._store = store
        self._clock = clock
        self._ids = id_generator

    def on_ack(self, command_id: str, session_id: str, command_type: str,
               source_event_id: str | None, trigger_text: str | None) -> None:
        # Only voice commands carry transcript provenance. A /agent-issued
        # command has no source_event_id -> out of scope for this writer.
        if source_event_id is None:
            return
        label = _COMMAND_TEXT.get(command_type, command_type)
        text = f'{label} (you said: "{trigger_text}")' if trigger_text else label
        atom = MemoryAtom(
            atom_id=command_id,  # idempotency key = command id
            session_id=session_id,
            source_event_id=source_event_id,
            kind="command_event",
            text=text,
            created_at=self._clock.now(),
            start_ms=0,
            source_pipeline_version="command",
        )
        stored = self._store.append(atom)
        if stored:
            logger.info("command_memory wrote command_event command=%s type=%s",
                        command_id, command_type)
