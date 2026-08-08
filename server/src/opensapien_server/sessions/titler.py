"""Name a closed segment from its transcript (spec §2.2).

The design shows recordings by name ("Studio standup · 01:04") on Home, on
Recordings, in the detail header, and inside every memory's source line. A
segment has only a ``preview`` — its first line of speech — which reads as an
accident rather than a name.

Titling hangs off **segment close**, not session end: a session lasts hours to
days (spec D1) and ``bye`` is unreliable (spec §0.3), so close is the only
signal that both fires reliably and marks something complete enough to name.

Every failure here is silent and the client falls back to ``preview``. A
missing title is a cosmetic degradation; a titler that raises into the sweep
would stop segments closing at all.
"""
from __future__ import annotations

import logging

from ..events.store import EventStore
from .segment_meta import TITLE_MAX_CHARS, SegmentMetaStore
from .segments import Segment

log = logging.getLogger(__name__)

# How much transcript the model sees. The opening of a conversation says what
# it is about; feeding the whole thing costs tokens for no gain and risks
# titling a segment after its last tangent.
MAX_LINES = 30
MAX_CHARS = 4000

_SYSTEM = (
    "You name recorded conversations. Reply with a title of at most 6 words: "
    "no quotes, no trailing punctuation, no preamble, no explanation. "
    "Reply with the title and nothing else."
)
_USER = "Transcript excerpt:\n{transcript}"


def _clean(raw: str) -> str | None:
    """Take the model's reply at arm's length.

    Instruction-following is not guaranteed, so anything that looks like a
    preamble, a multi-line answer, or a wrapped quotation gets normalised —
    and anything left empty or absurdly long is rejected outright rather
    than rendered as a recording's name.
    """
    body = (raw or "").strip()
    if not body:
        return None
    title = body.splitlines()[0].strip()
    # Loop rather than a fixed sequence of strips: a reply like
    # `"Studio standup".` interleaves quote and punctuation, so one pass in
    # any fixed order leaves a stray character behind.
    while True:
        stripped = title.strip().strip('"').strip("'").rstrip(".!,").strip()
        if stripped == title:
            break
        title = stripped
    if not title or len(title) > TITLE_MAX_CHARS:
        return None
    return title


class SegmentTitler:
    """Titles closed segments using the shared chat model.

    ``llm_chat`` is the same ``OpenAICompatibleChatModel`` the extractor uses
    (spec ground rule 6) — one model, one endpoint, one place to configure.
    ``None`` disables titling entirely, which is how every existing test and
    any deployment without an LLM keeps working.
    """

    def __init__(
        self,
        *,
        events: EventStore,
        meta: SegmentMetaStore,
        llm_chat=None,
    ) -> None:
        self._events = events
        self._meta = meta
        self._llm = llm_chat

    def title_segment(self, segment: Segment) -> str | None:
        """Generate and store a title. Returns it, or None if nothing was written.

        Skips a segment that already has a title — including one the user
        wrote, which the store also refuses to overwrite. Both checks exist
        because the cheap one saves an LLM call and the durable one is what
        actually guarantees the rule under concurrency.
        """
        if self._llm is None:
            return None
        existing = self._meta.get(segment.id)
        if existing is not None and existing.title:
            return None
        transcript = self._transcript_of(segment)
        if not transcript:
            return None
        try:
            reply = self._llm.complete(_SYSTEM, _USER.format(transcript=transcript))
        except Exception:
            log.warning("segment_title_failed segment=%s", segment.id, exc_info=True)
            return None
        title = _clean(reply)
        if title is None:
            log.debug("segment_title_unusable segment=%s reply=%r", segment.id, reply)
            return None
        if not self._meta.set_title(segment.id, title, source="llm"):
            return None  # a user title won the race
        log.info("segment_titled segment=%s title=%r", segment.id, title)
        return title

    def _transcript_of(self, segment: Segment) -> str:
        lines: list[str] = []
        total = 0
        for event in self._events.events(segment.session_id):
            if event.seq < segment.first_seq:
                continue
            if event.seq > segment.last_seq:
                break
            text = (event.text or "").strip()
            if not text:
                continue
            lines.append(text)
            total += len(text)
            if len(lines) >= MAX_LINES or total >= MAX_CHARS:
                break
        return "\n".join(lines)
