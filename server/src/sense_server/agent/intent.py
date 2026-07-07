"""Async wrapper around the OpenAI-compatible chat model (N2.1 / H4).

The :class:`AgentLLM` is the single seam the Planner calls. It is
**async** so the event loop is never blocked by the synchronous
``OpenAICompatibleChatModel.complete`` call (H4: ten concurrent
``POST /agent`` requests must not serialize on the LLM call).

The implementation runs the blocking call in
:func:`asyncio.to_thread` and parses the raw text into an
:class:`~sense_server.contracts.types.AgentAction`. A parse failure
returns an :class:`LLMResult` with ``parsed=None`` and the parse
error string; the Validator then rejects with INVALID_JSON.
"""
from __future__ import annotations

import asyncio
import json
from typing import Protocol, runtime_checkable

from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    LLMResult,
    Prompt,
)


@runtime_checkable
class AgentLLM(Protocol):
    """Single seam for "ask the LLM". Async so it can be composed with
    other async work without blocking the event loop (H4)."""

    name: str
    version: str

    async def reason(self, prompt: Prompt) -> LLMResult:
        """Return the raw text + best-effort parsed :class:`AgentAction`."""
        ...


class OpenAICompatibleAgentLLM:
    """Async adapter over :class:`sense_server.memory.llm.OpenAICompatibleChatModel`.

    The synchronous ``complete`` call is dispatched to a worker thread
    via :func:`asyncio.to_thread` so aiohttp request handlers don't
    block on it.
    """

    name = "openai_compatible"
    version = "v1"

    def __init__(self, chat) -> None:
        """``chat`` is any object with a synchronous ``complete(system, user)`` method."""
        self._chat = chat

    async def reason(self, prompt: Prompt) -> LLMResult:
        try:
            raw = await asyncio.to_thread(
                self._chat.complete, prompt.system, prompt.user
            )
        except Exception as e:
            return LLMResult(raw="", parsed=None, parse_error=f"transport: {e!r}")
        return _parse(raw)


def _parse(raw: str) -> LLMResult:
    """Best-effort parse of the LLM raw text into an :class:`AgentAction`.

    A failed parse returns ``parsed=None`` and the error message; the
    Validator then rejects with INVALID_JSON.
    """
    if not raw or not raw.strip():
        return LLMResult(raw=raw or "", parsed=None, parse_error="empty response")
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        return LLMResult(raw=raw, parsed=None, parse_error=f"json: {e}")
    if not isinstance(data, dict):
        return LLMResult(raw=raw, parsed=None, parse_error="not a JSON object")
    try:
        kind = AgentActionKind(data["kind"])
    except (KeyError, ValueError) as e:
        return LLMResult(raw=raw, parsed=None, parse_error=f"kind: {e}")
    text = data.get("text", "")
    atom_ids = tuple(data.get("atom_ids", ()))
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError) as e:
        return LLMResult(raw=raw, parsed=None, parse_error=f"confidence: {e}")
    return LLMResult(
        raw=raw,
        parsed=AgentAction(
            kind=kind,
            text=text,
            atom_ids=atom_ids,
            confidence=confidence,
        ),
        parse_error=None,
    )
