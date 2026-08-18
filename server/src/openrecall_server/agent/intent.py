"""Async wrapper around the OpenAI-compatible chat model (N2.1 / H4).

The :class:`AgentLLM` is the single seam the Planner calls. It is
**async** so the event loop is never blocked by the synchronous
``OpenAICompatibleChatModel.complete`` call (H4: ten concurrent
``POST /agent`` requests must not serialize on the LLM call).

The implementation runs the blocking call in
:func:`asyncio.to_thread` and parses the raw text into an
:class:`~openrecall_server.contracts.types.AgentAction`. A parse failure
returns an :class:`LLMResult` with ``parsed=None`` and the parse
error string; the Validator then rejects with INVALID_JSON.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

from pydantic import ValidationError

from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    IssueCommandPayload,
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
    """Async adapter over :class:`openrecall_server.memory.llm.OpenAICompatibleChatModel`.

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

    For ``kind=issue_command`` replies, the ``command`` field is
    validated against the :class:`IssueCommandPayload` schema
    (5-type allowlist, required ``idempotency_key``, confidence
    range). A missing or invalid ``command`` field on an
    issue_command reply is a parse failure — the Validator cannot
    accept a command action without a payload.

    For non-issue_command replies, a stray ``command`` field is
    silently ignored: ``AgentAction.command`` stays ``None`` so an
    answer / no_memory reply is never accidentally a command.
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

    # P1: server-side action payloads. create_memory carries an optional
    # memory_kind (the atom kind: fact/task/preference/event); the planner
    # defaults to "fact" at mint time. create_reminder carries a required
    # due_at (ISO 8601); the LLM resolves relative times ("at 6pm", "in an
    # hour") against the server wall clock. A missing/unparseable due_at is
    # a parse failure — the validator cannot accept a reminder without one.
    memory_kind: str | None = None
    due_at: datetime | None = None
    if kind in (AgentActionKind.CREATE_MEMORY, AgentActionKind.CREATE_REMINDER):
        memory_kind = data.get("memory_kind")
        if kind == AgentActionKind.CREATE_REMINDER:
            raw_due = data.get("due_at")
            if not raw_due:
                return LLMResult(
                    raw=raw, parsed=None,
                    parse_error="create_reminder reply missing 'due_at'",
                )
            try:
                due_at = datetime.fromisoformat(raw_due)
            except (TypeError, ValueError) as e:
                return LLMResult(
                    raw=raw, parsed=None, parse_error=f"due_at: {e}",
                )
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)

    # P2-commands: parse the structured command payload for
    # issue_command. The 5-type allowlist and per-type param bounds
    # are enforced by IssueCommandPayload (Literal type) — the
    # downstream StrictCommandValidator is the sole authority for
    # the runtime semantics; this parser only enforces the wire
    # shape. A non-issue_command reply must not carry a command
    # payload; if it does, it is silently ignored.
    #
    # Wire-shape note: the v2 system prompt puts ``confidence`` at
    # the top level alongside ``kind``, not inside the ``command``
    # object (which carries ``command_type``, ``params``, and
    # ``idempotency_key``). The parser reconciles the two by
    # injecting the top-level confidence into the payload before
    # model_validate, so the existing IssueCommandPayload schema
    # (and the downstream ValidatedCommand / StrictCommandGuardrails
    # that read payload.confidence) keep working without a contract
    # change. The LLM never has to know about the internal type.
    command: IssueCommandPayload | None = None
    if kind == AgentActionKind.ISSUE_COMMAND:
        command_obj = data.get("command")
        if command_obj is None:
            return LLMResult(
                raw=raw,
                parsed=None,
                parse_error="issue_command reply missing 'command' payload",
            )
        if not isinstance(command_obj, dict):
            return LLMResult(
                raw=raw,
                parsed=None,
                parse_error=f"issue_command 'command' must be an object, got {type(command_obj).__name__}",
            )
        # Inject the top-level confidence. IssueCommandPayload's
        # extra='forbid' would reject it on the payload if the LLM
        # included it on the command object, so we strip any
        # duplicate from command_obj first.
        cleaned = {k: v for k, v in command_obj.items() if k != "confidence"}
        try:
            command = IssueCommandPayload.model_validate(
                {**cleaned, "confidence": confidence}
            )
        except ValidationError as e:
            return LLMResult(
                raw=raw,
                parsed=None,
                parse_error=f"command payload: {e}",
            )

    return LLMResult(
        raw=raw,
        parsed=AgentAction(
            kind=kind,
            text=text,
            atom_ids=atom_ids,
            confidence=confidence,
            command=command,
            memory_kind=memory_kind,
            due_at=due_at,
        ),
        parse_error=None,
    )
