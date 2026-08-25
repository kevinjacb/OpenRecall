"""Speech→command detector: two-stage, confirmed-wearer-gated.

Stage 1 is a free synchronous phrase match over a rolling recent-text buffer
(:func:`match_command_phrase`). Stage 2 is a single scoped LLM call that
returns a structured command or null. Confirmed commands dispatch through
the existing ``StrictCommandValidator`` → ``StrictCommandGuardrails`` →
``CommandDispatcher.issue`` chain — no Planner, no retrieval. A command
memory is written on device ack (see :mod:`command_memory`).
"""
from __future__ import annotations


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