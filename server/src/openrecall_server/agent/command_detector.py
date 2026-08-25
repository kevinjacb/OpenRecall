"""Speech→command detector: two-stage, confirmed-wearer-gated.

Stage 1 is a free synchronous phrase match over a rolling recent-text buffer
(:func:`match_command_phrase`). Stage 2 is a single scoped LLM call that
returns a structured command or null. Confirmed commands dispatch through
the existing ``StrictCommandValidator`` → ``StrictCommandGuardrails`` →
``CommandDispatcher.issue`` chain — no Planner, no retrieval. A command
memory is written on device ack (see :mod:`command_memory`).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

_ROLLING_BUFFER_SEGS = 10


def match_command_phrase(buffer: str, phrases: dict[str, str]) -> str | None:
    """Return the command type whose phrase appears in ``buffer``, longest
    phrase first, or ``None``. Case-insensitive. Bare common words are not
    keys by design (see ``DEFAULT_COMMAND_PHRASES``)."""
    if not phrases:
        return None
    low = buffer.lower()
    # Longest phrase first so a longer key wins over a shorter prefix.
    for phrase in sorted(phrases, key=len, reverse=True):
        if phrase in low:
            return phrases[phrase]
    return None


class CommandDetector:
    """Two-stage speech→command detector, confirmed-wearer-gated.

    ``feed`` runs synchronously on the audio path: wearer gate → rolling
    buffer → Stage 1 phrase match → cooldown/in-flight dedup → schedule
    Stage 2 off the audio path via the loop. Stage 2 (LLM + dispatch) is
    :meth:`_run_stage2`, implemented in Task 5.
    """

    def __init__(
        self,
        *,
        model,
        dispatcher,
        command_validator,
        capability_provider,
        speaker_registry,
        loop: asyncio.AbstractEventLoop,
        config: CommandDetectorConfig,
        ids,
        clock,
    ) -> None:
        self._model = model
        self._dispatcher = dispatcher
        self._validator = command_validator
        self._caps = capability_provider
        self._speaker_registry = speaker_registry
        self._loop = loop
        self._cfg = config
        self._ids = ids
        self._clock = clock
        # session_id -> deque of recent segment texts (rolling buffer).
        self._buffers: dict[str, deque[str]] = {}
        # session_id -> {command_type: last_candidate_monotonic}
        self._last_candidate: dict[str, dict[str, float]] = {}
        self._inflight: dict[str, int] = {}
        # command_id -> source_event_id (for the ack-time memory).
        self._provenance: dict[str, str] = {}

    def feed(self, session_id: str, event: Any) -> None:
        if not self._cfg.enabled:
            return
        # --- confirmed-wearer gate ---
        if self._cfg.require_wearer:
            spk = self._speaker_registry.get(event.speaker) if (
                event.speaker and self._speaker_registry is not None) else None
            if not (event.speaker and event.speaker_assignment == "confirmed"
                    and spk is not None and spk.is_wearer):
                return
        # --- rolling recent-text buffer ---
        buf = self._buffers.setdefault(session_id, deque(maxlen=_ROLLING_BUFFER_SEGS))
        buf.append(event.text or "")
        joined = " ".join(buf)
        ctype = match_command_phrase(joined, self._cfg.phrases)
        if ctype is None:
            return
        # --- cooldown dedup (per session, per matched command type) ---
        now = time.monotonic()
        last = self._last_candidate.setdefault(session_id, {})
        if now - last.get(ctype, -self._cfg.cooldown_s) < self._cfg.cooldown_s:
            logger.debug("command_detector dedup session=%s type=%s", session_id, ctype)
            return
        # --- in-flight cap (defends the shared Ollama model) ---
        if self._inflight.get(session_id, 0) >= self._cfg.max_inflight:
            logger.info("command_detector in-flight cap session=%s; dropping", session_id)
            return
        last[ctype] = now
        self._inflight[session_id] = self._inflight.get(session_id, 0) + 1
        context = joined  # Stage 2 sees the rolling buffer as context
        self._loop.call_soon_threadsafe(
            self._launch_stage2, session_id, event, ctype, context)

    def _launch_stage2(self, session_id: str, event: Any, ctype: str, context: str) -> None:
        """Runs on the gateway loop; kicks off the async Stage 2 + dispatch."""
        try:
            asyncio.ensure_future(self._run_stage2(session_id, event, ctype, context))
        except RuntimeError:
            # No running loop in some test contexts; fall back to a task.
            asyncio.create_task(self._run_stage2(session_id, event, ctype, context))

    async def _run_stage2(self, session_id: str, event: Any, ctype: str, context: str) -> None:
        """Task 5 implements the LLM confirmation + dispatch."""
        raise NotImplementedError


# --- Task 4: Stage 2 prompt builder + reply parser (pure) -------------------

@dataclass(frozen=True)
class Stage2Result:
    command_type: str | None
    params: dict
    confidence: float


_SYSTEM_TEMPLATE = (
    "You classify whether a wearer is directly commanding their own wearable "
    "device. Narration, quotes, questions, hypotheticals, and third-person "
    "mentions are NOT commands. If it is a direct command, output JSON "
    '{{"command": {{"type": <one of: {types}>, "params": {{}}}}, '
    '"confidence": <0..1>}}. If not, output '
    '{{"command": null, "confidence": <0..1>}}. params is always {{}} for these '
    "types. Output only the JSON."
)


def stage2_messages(context: str, allowed_types: tuple[str, ...]) -> tuple[str, str]:
    system = _SYSTEM_TEMPLATE.format(types=", ".join(allowed_types))
    user = f"Recent transcript:\n{context}\n\nClassify the last sentence."
    return system, user


def parse_stage2_reply(raw: str, allowed_types: tuple[str, ...]) -> Stage2Result:
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return Stage2Result(None, {}, 0.0)
    if not isinstance(obj, dict):
        return Stage2Result(None, {}, 0.0)
    conf = obj.get("confidence")
    if not isinstance(conf, (int, float)) or not (0.0 <= conf <= 1.0):
        return Stage2Result(None, {}, 0.0)
    cmd = obj.get("command")
    if cmd is None:
        return Stage2Result(None, {}, float(conf))
    if not isinstance(cmd, dict):
        return Stage2Result(None, {}, float(conf))
    ctype = cmd.get("type")
    params = cmd.get("params", {})
    if ctype not in allowed_types or not isinstance(params, dict):
        return Stage2Result(None, {}, float(conf))
    return Stage2Result(ctype, params, float(conf))