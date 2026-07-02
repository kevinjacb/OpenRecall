"""Model-agnostic memory extraction.

The wearable's transcripts are turned into structured memories by a *pluggable*
``ChatModel``. The model is never hardcoded: any OpenAI-compatible endpoint works —
a local model (Gemma/Qwen via Ollama or ``mlx_lm.server``) or a cloud model
(MiniMax/OpenAI/…). Swapping models is a config change, not a code change.

``LLMExtractor`` owns only the prompt and the *tolerant* parsing of the reply, both
of which are pure and unit-tested with a fake model. The concrete OpenAI-compatible
client lives in :mod:`sense_server.memory.llm`.
"""

from __future__ import annotations

import json
import re
from typing import Protocol, runtime_checkable

from pydantic import BaseModel, ValidationError

_JSON_ARRAY = re.compile(r"\[.*\]", re.DOTALL)

DEFAULT_PROMPT = (
    "You extract durable, factual memories from a short transcript of someone's day. "
    "Return ONLY a JSON array; each element is an object with string fields "
    '"kind" (e.g. fact, task, preference, event) and "text" (a concise, '
    "self-contained memory written in the third person). If nothing is worth "
    "remembering, return []. Do not include any prose outside the JSON array."
)


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
        return self._parse(self._model.complete(self._prompt, text))

    @staticmethod
    def _parse(reply: str) -> list[ExtractedMemory]:
        """Tolerantly pull a JSON array of memories out of a model reply.

        Handles code fences and surrounding prose, ignores non-object elements and
        items missing fields, and never raises on a bad completion (returns []).
        """
        match = _JSON_ARRAY.search(reply)
        if match is None:
            return []
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []
        if not isinstance(data, list):
            return []

        memories: list[ExtractedMemory] = []
        for item in data:
            if not isinstance(item, dict):
                continue
            try:
                memories.append(ExtractedMemory(kind=item["kind"], text=item["text"]))
            except (KeyError, ValidationError):
                continue
        return memories
