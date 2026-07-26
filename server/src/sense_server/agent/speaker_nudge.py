"""Speaker-naming nudges — a listener on the extraction worker.

Fires two nudges through the existing ProactiveOutbox -> ProactiveMessage path:

* **Confirm nudge**: the implicit "You" (cold-start tag) crosses
  ``confirm_turns`` -> "I've been hearing one main voice — is that you?".
* **Name nudge**: a corroborated but unnamed unknown crosses
  ``name_nudge_turns`` -> "want to name them?", carrying 2-3 sample
  transcript lines and a ``propose={kind:"name_speaker", speaker_id}`` so
  the phone can render a quick input.

Dedupes per ``speaker_id`` (once per unknown, not every session). Respects
``SENSE_RATE_LIMIT_PER_MIN``. Never fires when ``SENSE_SPEAKER_ENABLED=false``.
Best-effort: never re-raises (the worker must not see listener exceptions).
"""
from __future__ import annotations

import logging

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..events.store import EventStore
from ..ingest.speaker_config import SpeakerConfig
from ..memory.extraction_worker import SessionCompletion
from ..memory.speaker_registry import SpeakerRegistry
from .proactive import WsSender

log = logging.getLogger(__name__)


class SpeakerNudgeListener:
    """Listener for :class:`SessionCompletion`; fires confirm/name nudges."""

    def __init__(
        self,
        registry: SpeakerRegistry,
        events: EventStore,
        ws_sender: WsSender,
        cfg: SpeakerConfig,
        *,
        rate_limit_per_min: int,
        ids: IdGenerator,
        clock: Clock,
    ) -> None:
        self._registry = registry
        self._events = events
        self._ws = ws_sender
        self._cfg = cfg
        self._rate_limit = rate_limit_per_min
        self._ids = ids
        self._clock = clock
        self._nudged: set[str] = set()  # dedupe per speaker_id
        # rolling per-minute send timestamps for the rate limit
        self._send_times: list[float] = []

    def set_ws_sender(self, ws_sender: WsSender) -> None:
        """Rebind the ws_sender. The gateway rebinds this per-connection so
        the nudge is pushed through whichever GatewayCore is active."""
        self._ws = ws_sender

    async def on_session_completion(self, completion: SessionCompletion) -> None:
        """Best-effort; never re-raises (the worker must not see listener errors)."""
        try:
            if not self._cfg.enabled:
                return
            for s in self._registry.list_speakers():
                if not self._allow():
                    log.debug("speaker_nudge_rate_limited")
                    return
                # Confirm nudge for the implicit cold-start "You".
                if (
                    s.is_wearer
                    and s.enrollment_status == "implicit"
                    and s.turn_count >= self._cfg.confirm_turns
                    and s.speaker_id not in self._nudged
                ):
                    await self._send(
                        completion.session_id,
                        "I've been hearing one main voice — is that you?",
                        propose=None,
                        mark=s.speaker_id,
                    )
                    continue
                # Name nudge for corroborated but unnamed unknowns.
                if (
                    s.display_name is None
                    and not s.is_wearer
                    and s.turn_count >= self._cfg.name_nudge_turns
                    and s.speaker_id not in self._nudged
                ):
                    lines = self._sample_lines(completion.session_id, s.speaker_id)
                    snippet = "  ".join(lines) if lines else ""
                    text = (
                        "I noticed you've spoken with the same person a few "
                        f"times — want to name them? {snippet}"
                    ).strip()
                    await self._send(
                        completion.session_id,
                        text,
                        propose={"kind": "name_speaker", "speaker_id": s.speaker_id},
                        mark=s.speaker_id,
                    )
        except Exception:
            log.exception("speaker_nudge_failed")

    def _sample_lines(self, session_id: str, speaker_id: str) -> list[str]:
        evs = self._events.events(session_id)
        attributed = [e.text for e in evs if e.speaker == speaker_id and e.text]
        return attributed[:3]

    async def _send(self, session_id: str, text: str, *, propose, mark: str) -> None:
        self._nudged.add(mark)
        self._send_times.append(self._clock.now().timestamp())
        await self._ws.send_proactive(
            session_id=session_id,
            request_id=self._ids.new(),
            text=text,
            atoms=(),
            propose=propose,
        )

    def _allow(self) -> bool:
        now = self._clock.now().timestamp()
        self._send_times = [t for t in self._send_times if now - t < 60.0]
        return len(self._send_times) < self._rate_limit