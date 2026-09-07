"""A PlannerLike backed by an out-of-process agent (spec §5.1, §5.4).

The seam is deliberate: `PlannerLike` has one method and two call sites, so
swapping the reasoning layer is an object swap and rolling back is one config
value. What does NOT move is authority — this planner opens a scoped ledger
entry, lets the agent read through MCP tools, and then builds its result from
**the ledger**, never from the transport's stdout. Only the ledger's answer
passed the citation gate.

Phase 2 ships `FakeTransport` only. No Hermes process is installed or run.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Awaitable, Callable, Protocol, runtime_checkable

from ..contracts.types import (
    PlannerContext, PlannerOutcome, PlannerResult, Proactive, RejectionReason,
    UserRequest,
)
from ..mcp.ledger import RequestLedger

log = logging.getLogger(__name__)

_PROMPT = """\
You are answering on behalf of a wearable memory device's owner.

Your request id is {request_id}. Every tool call must carry it.

Search the owner's memory before answering. You may cite ONLY atom ids that
memory.search or memory.get returned to you during THIS request — a citation
you did not retrieve is refused.

Finish by calling agent.respond exactly once with your final answer.

The owner asked: {text}
"""


@runtime_checkable
class HermesTransport(Protocol):
    """How a prompt reaches the agent. The only thing that varies per phase."""

    async def run(self, prompt: str, *, request_id: str,
                  timeout_s: float) -> str: ...


class FakeTransport:
    """Test double. `on_run` is an async (prompt, request_id) -> str callable,
    so a test can drive real MCP tool calls from inside the "agent"."""

    def __init__(self, on_run: Callable[[str, str], Awaitable[str]]) -> None:
        self._on_run = on_run

    async def run(self, prompt: str, *, request_id: str,
                  timeout_s: float) -> str:
        return await asyncio.wait_for(
            self._on_run(prompt, request_id), timeout=timeout_s)


class HermesPlanner:
    """Implements PlannerLike (`async def plan(ctx) -> PlannerResult`)."""

    def __init__(self, *, transport: HermesTransport, ledger: RequestLedger,
                 clock, ids, timeout_s: float = 45.0,
                 proactive_timeout_s: float = 90.0,
                 strict_provenance: bool = False) -> None:
        self._transport = transport
        self._ledger = ledger
        self._clock = clock
        self._ids = ids
        self._timeout_s = timeout_s
        self._proactive_timeout_s = proactive_timeout_s
        self._strict = strict_provenance

    def build_prompt(self, ctx: PlannerContext) -> str:
        text = getattr(ctx.trigger, "text", None) or getattr(
            ctx.trigger, "transcript", "") or ""
        return _PROMPT.format(request_id=ctx.request_id, text=text)

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        proactive = isinstance(ctx.trigger, Proactive)
        trigger_kind = "proactive" if proactive else "user_request"
        timeout_s = self._proactive_timeout_s if proactive else self._timeout_s
        # The ledger entry is what re-imposes the proactive ISSUE_COMMAND
        # prohibition (planner.py:231) across a process boundary.
        self._ledger.open(ctx.request_id, session_id=ctx.session_id,
                          trigger_kind=trigger_kind,
                          ttl_s=int(timeout_s) + 30)
        try:
            await self._transport.run(
                self.build_prompt(ctx), request_id=ctx.request_id,
                timeout_s=timeout_s)
            response = self._ledger.response(ctx.request_id)
        finally:
            # Always close: a leaked entry is a request_id an agent could keep
            # using after its run ended.
            self._ledger.close(ctx.request_id)

        if response is None:
            return self._no_response(ctx)
        return PlannerResult(
            request_id=ctx.request_id,
            retrieval_trace_id=self._ids.new(),
            outcome=_OUTCOME_BY_KIND.get(response.kind, PlannerOutcome.RETURN),
            answer=response.text or None,
            confidence=response.confidence,
            atom_ids=response.atom_ids,
            command_id=response.command_id,
            memory_atom_id=response.memory_atom_id,
            reminder_id=response.reminder_id,
        )

    def _no_response(self, ctx: PlannerContext) -> PlannerResult:
        """The agent exited without calling agent.respond.

        Nothing passed the citation gate, so there is no grounded answer. Under
        strict provenance that is a refusal; otherwise it is surfaced as
        explicitly unverified so the UI renders it without citation chips.
        """
        log.warning("hermes_unstructured_response request_id=%s", ctx.request_id)
        if self._strict:
            return PlannerResult(
                request_id=ctx.request_id,
                retrieval_trace_id=self._ids.new(),
                outcome=PlannerOutcome.REFUSE,
                refusal_reason=RejectionReason.PROVENANCE_MISSING,
                refusal_message="the agent returned no provenance-checked answer",
            )
        return PlannerResult(
            request_id=ctx.request_id,
            retrieval_trace_id=self._ids.new(),
            outcome=PlannerOutcome.RETURN_WITH_UNCERTAINTY,
            answer=None,
            confidence=None,
            confidence_band="unverified",
        )


_OUTCOME_BY_KIND = {
    "answer": PlannerOutcome.RETURN,
    "no_memory": PlannerOutcome.RETURN,
    "issue_command": PlannerOutcome.ISSUE_COMMAND,
    "create_memory": PlannerOutcome.CREATE_MEMORY,
    "create_reminder": PlannerOutcome.CREATE_REMINDER,
}
