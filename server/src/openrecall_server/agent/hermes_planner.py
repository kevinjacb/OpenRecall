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
                 strict_provenance: bool = False,
                 confidence_autonomous: float = 0.85,
                 confidence_confirm: float = 0.60) -> None:
        self._transport = transport
        self._ledger = ledger
        # Forward plumbing: not read yet. A later task (audit_id / timestamped
        # ledger correlation) is expected to use this; kept as a constructor
        # dependency now rather than added as a breaking change later.
        self._clock = clock
        self._ids = ids
        self._timeout_s = timeout_s
        self._proactive_timeout_s = proactive_timeout_s
        self._strict = strict_provenance
        # Mirrors agent/guardrails.py ConfidenceGateGuardrails' two
        # thresholds exactly (defaults match GuardrailsConfig). Task 4 wires
        # these from config; here they're plain constructor kwargs so the
        # gate is unit-testable without config plumbing.
        self._confidence_autonomous = confidence_autonomous
        self._confidence_confirm = confidence_confirm

    def build_prompt(self, ctx: PlannerContext) -> str:
        text = getattr(ctx.trigger, "text", None) or getattr(
            ctx.trigger, "transcript", "") or ""
        return _PROMPT.format(request_id=ctx.request_id, text=text)

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        proactive = isinstance(ctx.trigger, Proactive)
        trigger_kind = "proactive" if proactive else "user_request"
        timeout_s = self._proactive_timeout_s if proactive else self._timeout_s
        # The ledger entry RECORDS trigger_kind so a proactive run is
        # distinguishable from a user_request one across a process boundary
        # (RequestLedger.may_issue_command reads it) — it does not yet
        # ENFORCE anything: may_issue_command has zero callers in src/, and
        # issue_command is not an accepted agent.respond kind (RESPONSE_KINDS
        # in mcp/tools.py). The proactive ISSUE_COMMAND prohibition
        # (planner.py:231) becomes live across this boundary only when
        # Phase 4 re-admits issue_command as a response kind and wires a
        # caller to this check.
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

        # NO_MEMORY mirrors agent/guardrails.py's ConfidenceGateGuardrails
        # short-circuit exactly (checked BEFORE the confidence gate there,
        # and before rate-limiting): it is always a REFUSE with
        # NO_SUPPORTING_MEMORY and the same canned message, never an
        # autonomous RETURN.
        if response.kind == "no_memory":
            return PlannerResult(
                request_id=ctx.request_id,
                retrieval_trace_id=self._ids.new(),
                outcome=PlannerOutcome.REFUSE,
                refusal_reason=RejectionReason.NO_SUPPORTING_MEMORY,
                refusal_message="No relevant memory found.",
            )

        if response.kind == "answer":
            outcome, refusal_reason, refusal_message = self._gate_confidence(
                response.confidence)
            if outcome is PlannerOutcome.REFUSE:
                # Mirrors planner.py:602-603 — a REFUSE carries no answer and
                # no confidence, only the refusal reason/message.
                return PlannerResult(
                    request_id=ctx.request_id,
                    retrieval_trace_id=self._ids.new(),
                    outcome=outcome,
                    refusal_reason=refusal_reason,
                    refusal_message=refusal_message,
                )
            return PlannerResult(
                request_id=ctx.request_id,
                retrieval_trace_id=self._ids.new(),
                outcome=outcome,
                answer=response.text or None,
                confidence=response.confidence,
                atom_ids=response.atom_ids,
            )

        # RESPONSE_KINDS (mcp/tools.py) admits only "answer" and "no_memory"
        # at agent.respond's boundary. issue_command / create_memory /
        # create_reminder are deliberately NOT accepted there: those are
        # server-side MINTS in-process (the planner creates the record from
        # the action's text/due_at and produces the id), and Phase 2 exposes
        # no minting tool out of process to validate a self-reported
        # memory_atom_id/reminder_id/command_id against — admitting them
        # here would let an agent self-report a write it never performed.
        # They return in Phase 4 alongside the minting tools that can
        # validate them. Nothing agent.respond can produce reaches this
        # branch; treat an unexpected kind as unverified rather than crash.
        log.error("hermes_unknown_response_kind request_id=%s kind=%r",
                  ctx.request_id, response.kind)
        return PlannerResult(
            request_id=ctx.request_id,
            retrieval_trace_id=self._ids.new(),
            outcome=PlannerOutcome.RETURN_WITH_UNCERTAINTY,
            answer=None,
            confidence=None,
            confidence_band="unverified",
        )

    def _gate_confidence(
        self, confidence: float | None
    ) -> tuple[PlannerOutcome, RejectionReason | None, str | None]:
        """Mirrors ConfidenceGateGuardrails.decide's confidence tiers exactly
        (agent/guardrails.py): >= autonomous -> RETURN; >= confirm ->
        RETURN_WITH_UNCERTAINTY; below confirm -> REFUSE/NOT_AUTONOMOUS with
        guardrails' own message text.

        guardrails.py's gate is reached only after its NO_MEMORY
        short-circuit (handled separately above, before this is called) and
        its rate-limit check (out of scope for Phase 2 — HermesPlanner has no
        equivalent yet). It does NOT check atom_ids; the "must cite at least
        one atom" rule (NO_ATOM_CITED) lives in validator.py, a different
        module this gate does not reproduce.

        `confidence=None` has no guardrails.py precedent (AgentAction.confidence
        there is a required float) — AgentResponse.confidence is Optional, so
        an agent can omit it. Treated as a floor failure (REFUSE) rather than
        RETURN: the whole point of this gate is that an unverified confidence
        must never present as autonomous, and "no confidence reported" is the
        least verified case there is.
        """
        if confidence is not None and confidence >= self._confidence_autonomous:
            return PlannerOutcome.RETURN, None, None
        if confidence is not None and confidence >= self._confidence_confirm:
            return PlannerOutcome.RETURN_WITH_UNCERTAINTY, None, None
        return (PlannerOutcome.REFUSE, RejectionReason.NOT_AUTONOMOUS,
                "Confidence too low to answer.")

    def _no_response(self, ctx: PlannerContext) -> PlannerResult:
        """The agent exited without calling agent.respond.

        Nothing passed the citation gate, so there is no grounded answer. Under
        strict provenance that is a refusal; otherwise it is surfaced as
        explicitly unverified (confidence_band="unverified") — a value the UI
        must special-case rather than a "low"/"medium"/"high" score, since
        there is no confidence to report at all.
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
