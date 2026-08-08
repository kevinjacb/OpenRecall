"""Model-agnostic memory extraction.

The wearable's transcripts are turned into structured memories by a *pluggable*
``ChatModel``. The model is never hardcoded: any OpenAI-compatible endpoint works —
a local model (Gemma/Qwen via Ollama or ``mlx_lm.server``) or a cloud model
(MiniMax/OpenAI/…). Swapping models is a config change, not a code change.

``LLMExtractor`` owns only the prompt and the *strict* parsing of the reply, both
of which are pure and unit-tested with a fake model. The concrete OpenAI-compatible
client lives in :mod:`openrecall_server.memory.llm`.

Parse contract (strict):

- A valid JSON ``[]`` is the only successful no-memory result.
- Anything else — non-JSON, JSON that is not an array, an array whose items
  are not the contracted ``{"kind","text"}`` shape (including the LLM's
  positional ``["event","text"]`` 2-strings that some chat-tuned models
  emit) — raises :class:`LLMParseError`.
- The parser does not touch metrics. The :class:`ExtractionWorker` is the
  sole owner of the :data:`Metrics.LLM_PARSE_FAILURES_TOTAL` counter and
  increments it once per failure, tagged with the session id, then leaves
  the cursor unchanged and re-raises so the next enqueue / reconciliation
  retries the same events.

The previous tolerant-parser behavior (silently returning ``[]`` for
malformed replies) caused the live "atoms never form" incident: the
extractor's cursor advanced past transcripts that the LLM had answered in
a shape the parser couldn't read.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

log = logging.getLogger(__name__)

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)

DEFAULT_PROMPT = (
    "You extract durable, factual memories from a short transcript of someone's day. "
    "Respond with ONLY a JSON array — no prose, no code fences. Each element MUST be a "
    "JSON object with exactly two string fields: \"kind\" (one of: fact, task, "
    "preference, event) and \"text\" (a concise, self-contained memory written in the "
    'third person). Example: [{"kind":"fact","text":"Sarah is moving to Austin next '
    'month."}]. Never return a flat array of strings like ["fact","some text"] — each '
    "element must be an object with \"kind\" and \"text\" keys. If nothing is worth "
    "remembering, return []."
)


class LLMExtractError(Exception):
    """Base class for all errors raised by :class:`LLMExtractor`."""


class LLMParseError(LLMExtractError):
    """The LLM reply could not be parsed into the contracted shape.

    A subclass of :class:`LLMExtractError` so callers that want to catch
    the whole family (parse + upstream chat errors) can. The parser does
    not touch metrics; the worker catches this and increments
    :data:`Metrics.LLM_PARSE_FAILURES_TOTAL`.
    """


class ExtractedMemory(BaseModel):
    kind: str
    text: str


@runtime_checkable
class ChatModel(Protocol):
    """Any chat LLM. ``system`` is the instruction, ``user`` the transcript."""

    def complete(self, system: str, user: str) -> str: ...


@runtime_checkable
class Extractor(Protocol):
    def extract(self, text: str) -> list[ExtractedMemory]: ...


class LLMExtractor:
    def __init__(self, model: ChatModel, *, prompt: str = DEFAULT_PROMPT) -> None:
        self._model = model
        self._prompt = prompt

    def extract(self, text: str) -> list[ExtractedMemory]:
        # The single LLM call for one extraction window. Log input size +
        # a snippet, and the parsed result / parse error — this is the seam
        # where "tokens are being used" becomes visible: if you see many
        # calls with tiny inputs and [] outputs, the windowing/cursor is
        # feeding the LLM fragments (the live-window-holdback regression).
        log.info(
            "llm_extract_call chars=%d text=%r", len(text), text[:200],
        )
        reply = self._model.complete(self._prompt, text)
        log.debug("llm_extract_reply raw=%r", reply[:300])
        try:
            memories = self._parse(reply)
        except LLMParseError as e:
            log.warning("llm_extract_parse_failed error=%s reply=%r", e, reply[:200])
            raise
        log.info(
            "llm_extract_ok memories=%d kinds=%s",
            len(memories), [m.kind for m in memories],
        )
        return memories

    @staticmethod
    def _parse(reply: str) -> list[ExtractedMemory]:
        """Strictly parse a model reply into a list of :class:`ExtractedMemory`.

        Contract:

        - ``"[]"`` is the only successful empty result (the LLM said:
          "I looked at this transcript and there is nothing memorable").
        - Any other shape — non-JSON, JSON that is not an array, an
          array with at least one non-conforming item, or items
          missing ``kind`` / ``text`` — raises :class:`LLMParseError`.
        - A partial-success reply (e.g. a list with one valid dict and
          one positional 2-string) also raises :class:`LLMParseError`:
          silently dropping items would let a model that flipped format
          mid-session look like an empty extraction, advancing the
          cursor past memory that never got extracted.

        The parser does not own metrics; the
        :class:`ExtractionWorker` is the sole owner of
        :data:`Metrics.LLM_PARSE_FAILURES_TOTAL` and increments it
        once per failure, tagged with the session id.
        """
        match = _JSON_ARRAY.search(reply)
        if match is None:
            raise LLMParseError(
                f"reply did not contain a JSON array: {reply[:120]!r}"
            )
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise LLMParseError(f"reply JSON could not be decoded: {e}") from e
        if not isinstance(data, list):
            raise LLMParseError(
                f"reply top-level value is not an array: {type(data).__name__}"
            )

        # An empty array is the explicit "no memory" signal.
        if not data:
            return []

        memories: list[ExtractedMemory] = []
        for i, item in enumerate(data):
            if not isinstance(item, dict):
                raise LLMParseError(
                    f"reply item {i} is not an object: {type(item).__name__}"
                )
            try:
                memories.append(ExtractedMemory(kind=item["kind"], text=item["text"]))
            except (KeyError, ValidationError) as e:
                raise LLMParseError(
                    f"reply item {i} is missing or has invalid fields: {e}"
                ) from e
        return memories
