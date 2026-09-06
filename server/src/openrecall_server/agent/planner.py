"""The real Planner (N3.2 + P2-commands Phase 6).

The Planner orchestrates the read path AND the command path:

  1. RETRIEVE — call :class:`Retriever` for relevant atoms.
  2. BUILD CONTEXT — call :class:`ContextBuilder` with the trigger,
     retrieved atoms, and capabilities. The v3 system prompt (see
     :mod:`agent.context`) carves out device actions from the
     empty-retrieval no-memory directive, so direct commands reach
     the LLM even on a cold index.
  3. REASON — call the async :class:`AgentLLM` (H4 — doesn't block).
  4. VALIDATE — strict JSON schema + provenance enforcement.
  5. GUARDRAIL — confidence-gated autonomy + rate limit. The
     answer guardrails map a `no_memory` LLM reply to
     `NO_SUPPORTING_MEMORY`, preserving factual-question safety
     at the LLM+guardrails level rather than a planner-side
     short-circuit.
  6. DISPATCH (P2-commands) — if the LLM emitted ISSUE_COMMAND, run
     the command through CommandValidator + CommandGuardrails +
     CommandDispatcher.issue. Carry the command_id back to the caller
     so the Android UI can watch its lifecycle.
  7. AUDIT — best-effort write to the durable log (H3 — failure does
     not lose the response).

The Planner is **stateless** (INV-1): every call takes the same
arguments and returns the same result (modulo the freshly-minted
request_id / trace_id / audit_id from the injected
:class:`~openrecall_server.contracts.id_generator.IdGenerator`). No
instance state, no rate-limit counters held on ``self`` — they live on
the :class:`Guardrails` seam, which the caller can swap.
"""
from __future__ import annotations

import logging
import time
from typing import Protocol, runtime_checkable

from ..commands.dispatcher import CommandDispatcher
from ..commands.issue import validate_and_issue
from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.metrics import Metrics
from ..memory.atom import MemoryAtom
from ..memory.store import AtomStore

from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    GuardOutcome,
    GuardedAction,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Proactive,
    Prompt,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
    UserRequest,
    ValidatedAction,
    ValidatorContext,
)
from .guardrails_command import CommandGuardrails, RejectionReason as CommandRejectionReason
from .validator_command import CommandValidator

log = logging.getLogger(__name__)

_REFUSE = GuardOutcome.REFUSE


@runtime_checkable
class PlannerLike(Protocol):
    """The Planner's public surface — tests depend on this Protocol."""

    async def plan(self, ctx: PlannerContext) -> PlannerResult: ...


class Planner:
    """The read-path + command-path orchestrator. See module docstring."""

    def __init__(
        self,
        retriever: Retriever,
        context_builder: ContextBuilder,
        llm: AgentLLM,
        validator: Validator,
        guardrails: Guardrails,
        audit,
        metrics,
        capability_provider: CapabilityProvider,
        clock: Clock,
        ids: IdGenerator,
        # P2-commands: the command-path components. Optional so the
        # P2-answers slice (which doesn't dispatch commands) can wire a
        # Planner without them; tests that exercise IssueCommand must
        # pass all three.
        #
        # command_validator: NOT used to validate commands anymore — since
        # command dispatch was routed through commands.issue.validate_and_issue
        # (which builds its own StrictCommandValidator internally), this
        # injected instance is read only as the `is None` guard for "is the
        # command path configured?" in _dispatch_command. That is deliberate:
        # keeping the actual validator un-injectable inside validate_and_issue
        # is what makes the safety chain un-bypassable by a future untrusted
        # caller. Do not restore this parameter to being consulted for
        # validation.
        command_validator: CommandValidator | None = None,
        command_guardrails: CommandGuardrails | None = None,
        dispatcher: CommandDispatcher | None = None,
        # P1 instruction processor: server-side actions mint atoms directly.
        # atom_store is the same store the extraction worker populates, so a
        # create_memory atom is immediately queryable. recent_transcript feeds
        # the planner context so "make a memory out of that" can refer to what
        # was just said. Both optional so the P2-answers / P2-commands slices
        # that don't mint still wire a Planner without them.
        atom_store: "AtomStore | None" = None,
        reminder_store=None,
        recent_transcript=None,
    ) -> None:
        self._retriever = retriever
        self._context_builder = context_builder
        self._llm = llm
        self._validator = validator
        self._guardrails = guardrails
        self._audit = audit
        self._metrics = metrics
        self._caps = capability_provider
        self._clock = clock
        self._ids = ids
        self._command_validator = command_validator
        self._command_guardrails = command_guardrails
        self._dispatcher = dispatcher
        self._atom_store = atom_store
        self._reminder_store = reminder_store
        self._recent = recent_transcript

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        trigger_kind = type(ctx.trigger).__name__
        # 1. RETRIEVE
        t0 = time.monotonic()
        retrieved = self._do_retrieve(ctx)
        retrieval_latency = int((time.monotonic() - t0) * 1000)
        self._metrics.observe(Metrics.RETRIEVAL_LATENCY_MS, retrieval_latency)
        log.info(
            "planner_retrieve trigger=%s session=%s atoms=%d retrieval_ms=%d",
            trigger_kind, ctx.session_id, len(retrieved.atoms), retrieval_latency,
        )

        # P2-commands: do NOT short-circuit on empty retrieval. The
        # previous short-circuit saved one LLM call per empty-retrieval
        # POST /agent, but it was wrong for direct device-action
        # requests: a `/agent -d '{"text":"record a 3 second video"}'`
        # from a fresh session has no atoms yet, and the LLM is the
        # only place that can recognize it as a device action and
        # emit issue_command. Short-circuiting the LLM meant every
        # direct command on a cold index was refused.
        #
        # Factual-question safety is preserved at the prompt level:
        # the v2 system prompt (see agent/context.py) explicitly tells
        # the LLM that empty retrieval is a no-memory signal for
        # FACTUAL questions but is normal/expected for DEVICE actions.
        # The answer guardrails then map a `no_memory` LLM reply to
        # `NO_SUPPORTING_MEMORY` exactly as before, so the audit log
        # and the operator-visible refusal are unchanged for the
        # factual-question path.
        #
        # The cost is one LLM call per empty-retrieval request. The
        # empty-retrieval case is a fresh session or a fresh gateway,
        # not the steady state; the call is async and runs in the
        # background like every other LLM call.

        # 2. BUILD CONTEXT
        capabilities = self._caps.capabilities()
        recent_text = ""
        if self._recent is not None and ctx.session_id:
            try:
                recent_text = self._recent.recent(ctx.session_id)
            except Exception:
                recent_text = ""
        # P3: pass the Trigger envelope. The ContextBuilder is
        # source-agnostic; for UserRequest it uses trigger.text, for
        # Proactive it uses trigger.transcript. The proactive
        # ISSUE_COMMAND prohibition is enforced later in this method.
        prompt = self._context_builder.build(
            ctx.trigger, retrieved, capabilities, recent_transcript=recent_text,
        )

        # 4. REASON (async — H4)
        t1 = time.monotonic()
        llm_result = await self._llm.reason(prompt)
        llm_latency = int((time.monotonic() - t1) * 1000)
        self._metrics.observe(Metrics.LLM_LATENCY_MS, llm_latency)
        log.info(
            "planner_llm trigger=%s session=%s kind=%s confidence=%s "
            "parse_error=%s llm_ms=%d",
            trigger_kind, ctx.session_id,
            llm_result.parsed.kind if llm_result.parsed else None,
            llm_result.parsed.confidence if llm_result.parsed else None,
            llm_result.parse_error, llm_latency,
        )

        # 5. VALIDATE
        t2 = time.monotonic()
        validated = self._validator.validate(
            ValidatorContext(
                retrieved_atom_ids=tuple(a.atom_id for a in retrieved.atoms),
                no_memory_top_score_threshold=0.3,
            ),
            llm_result,
        )
        validator_latency = int((time.monotonic() - t2) * 1000)
        self._metrics.observe(Metrics.VALIDATOR_LATENCY_MS, validator_latency)
        if validated.rejection is not None:
            self._metrics.increment(
                Metrics.VALIDATOR_FAILURES_TOTAL,
                tags={"reason": str(validated.rejection)},
            )

        # 6. GUARDRAILS
        t3 = time.monotonic()
        guarded = self._guardrails.decide(validated, ValidatorContext(
            retrieved_atom_ids=tuple(a.atom_id for a in retrieved.atoms),
        ))
        guardrails_latency = int((time.monotonic() - t3) * 1000)
        self._metrics.observe(Metrics.GUARDRAILS_LATENCY_MS, guardrails_latency)

        # 7. DISPATCH (P2-commands) or ANSWER
        # The LLM may have emitted ISSUE_COMMAND; the action carries
        # the parsed IssueCommandPayload. Run the command through the
        # validator + guardrails + dispatcher, build a result with
        # command_id + command_status.
        if validated.action.kind == AgentActionKind.ISSUE_COMMAND:
            # P3: a Proactive trigger is FORBIDDEN from issuing a device
            # command. The user didn't ask — a hallucination on the
            # proactive path would become a real device action. This is
            # enforced here (not in CommandValidator, not in
            # CommandGuardrails, not in CommandDispatcher) as a
            # type-system guarantee: a reviewer can grep for the
            # prohibition and find exactly one place.
            if isinstance(ctx.trigger, Proactive):
                result = self._build_result(
                    ctx, retrieved,
                    outcome=PlannerOutcome.REFUSE,
                    guarded=GuardedAction(
                        outcome=_REFUSE,
                        action=validated.action,
                        refusal_reason=RejectionReason.PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND,
                        refusal_message="proactive triggers cannot issue device commands",
                    ),
                    retrieval_latency_ms=retrieval_latency,
                    llm_latency_ms=llm_latency,
                    validator_latency_ms=validator_latency,
                    guardrails_latency_ms=guardrails_latency,
                )
                self._safe_audit(result, prompt=prompt, ctx=ctx)
                return result
            return await self._dispatch_command(
                ctx, retrieved, validated, prompt,
                retrieval_latency, llm_latency, validator_latency, guardrails_latency,
            )

        # P1: server-side actions. These are NOT device commands; they
        # mint atoms directly and bypass StrictCommandGuardrails. They
        # ARE confidence-gated by the answer-path guardrails above (they
        # fall through to the rate limit + confidence gate like ANSWER).
        # A guardrail REFUSE means the confidence was too low — do not mint.
        if validated.action.kind == AgentActionKind.CREATE_MEMORY:
            if guarded.outcome == _REFUSE:
                result = self._build_result(
                    ctx, retrieved, outcome=PlannerOutcome.REFUSE, guarded=guarded,
                    retrieval_latency_ms=retrieval_latency, llm_latency_ms=llm_latency,
                    validator_latency_ms=validator_latency, guardrails_latency_ms=guardrails_latency,
                )
                self._safe_audit(result, prompt=prompt, ctx=ctx)
                return result
            return self._create_memory(
                ctx, retrieved, validated, guarded, prompt,
                retrieval_latency, llm_latency, validator_latency, guardrails_latency,
            )
        if validated.action.kind == AgentActionKind.CREATE_REMINDER:
            if guarded.outcome == _REFUSE:
                result = self._build_result(
                    ctx, retrieved, outcome=PlannerOutcome.REFUSE, guarded=guarded,
                    retrieval_latency_ms=retrieval_latency, llm_latency_ms=llm_latency,
                    validator_latency_ms=validator_latency, guardrails_latency_ms=guardrails_latency,
                )
                self._safe_audit(result, prompt=prompt, ctx=ctx)
                return result
            return self._create_reminder(
                ctx, retrieved, validated, guarded, prompt,
                retrieval_latency, llm_latency, validator_latency, guardrails_latency,
            )

        # 7b. ANSWER / NO_MEMORY path (P2-answers)
        if guarded.outcome.value == "return":
            outcome = PlannerOutcome.RETURN
        elif guarded.outcome.value == "return_with_uncertainty":
            outcome = PlannerOutcome.RETURN_WITH_UNCERTAINTY
        else:
            outcome = PlannerOutcome.REFUSE
            self._metrics.increment(Metrics.REFUSALS_TOTAL, tags={"kind": str(guarded.refusal_reason)})

        result = self._build_result(
            ctx, retrieved,
            outcome=outcome,
            guarded=guarded,
            retrieval_latency_ms=retrieval_latency,
            llm_latency_ms=llm_latency,
            validator_latency_ms=validator_latency,
            guardrails_latency_ms=guardrails_latency,
        )

        # 8. AUDIT (H3: best-effort)
        self._safe_audit(result, prompt=prompt, ctx=ctx)
        return result

    async def _dispatch_command(
        self,
        ctx: PlannerContext,
        retrieved: RetrievedContext,
        validated: ValidatedAction,
        prompt: Prompt,
        retrieval_latency_ms: int,
        llm_latency_ms: int,
        validator_latency_ms: int,
        guardrails_latency_ms: int,
    ) -> PlannerResult:
        """Run the LLM's IssueCommand through validator + guardrails +
        dispatcher, build a PlannerResult with command_id + command_status.

        H3: any failure in the dispatch path becomes a refusal rather
        than a crash. The user sees a friendly message; the audit log
        records the failure.
        """
        action = validated.action
        if action.command is None:
            # The LLM said "issue_command" but the parser didn't produce
            # a payload. Defensive: refuse rather than crash.
            return self._build_result(
                ctx, retrieved,
                outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE,
                    action=action,
                    refusal_reason=RejectionReason.SCHEMA_MISMATCH,
                    refusal_message="LLM emitted issue_command with no payload.",
                ),
                retrieval_latency_ms=retrieval_latency_ms,
                llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms,
                guardrails_latency_ms=guardrails_latency_ms,
            )
        if (self._command_validator is None or self._dispatcher is None
                or self._command_guardrails is None):
            # No command path wired (P2-answers default). Refuse with
            # a clear message; the operator should set up the command
            # components to enable IssueCommand.
            return self._build_result(
                ctx, retrieved,
                outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE,
                    action=action,
                    refusal_reason=RejectionReason.UNKNOWN,
                    refusal_message="command dispatch is not configured on this server",
                ),
                retrieval_latency_ms=retrieval_latency_ms,
                llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms,
                guardrails_latency_ms=guardrails_latency_ms,
            )

        # Validate -> guardrails -> sign, through the one choke point.
        # Rejection mapping stays here because PlannerResult speaks
        # RejectionReason from contracts, not the commands enum.
        issued = validate_and_issue(
            command_type=action.command.command_type,
            params=action.command.params,
            idempotency_key=action.command.idempotency_key,
            dispatcher=self._dispatcher,
            ids=self._ids,
            clock=self._clock,
            capability_provider=self._caps,
            session_id=ctx.session_id or "",
            confidence=action.command.confidence,
            confidence_autonomous=self._command_guardrails._autonomous,
            ttl=_default_ttl(),
        )
        if not issued.ok:
            if issued.stage == "guardrails":
                reason = _map_guardrail_rejection(issued.rejection)
            elif issued.stage == "validate":
                reason = _map_validator_rejection(issued.rejection)
            else:
                reason = RejectionReason.UNKNOWN
            return self._build_result(
                ctx, retrieved,
                outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE,
                    action=action,
                    refusal_reason=reason,
                    refusal_message=issued.message or "command rejected",
                ),
                retrieval_latency_ms=retrieval_latency_ms,
                llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms,
                guardrails_latency_ms=guardrails_latency_ms,
            )
        command_id = issued.signed.command.command_id

        result = self._build_result(
            ctx, retrieved,
            outcome=PlannerOutcome.ISSUE_COMMAND,
            guarded=guarded_action_for_command(action, command_id),
            retrieval_latency_ms=retrieval_latency_ms,
            llm_latency_ms=llm_latency_ms,
            validator_latency_ms=validator_latency_ms,
            guardrails_latency_ms=guardrails_latency_ms,
            command_id=command_id,
            command_status="PENDING",
        )
        self._safe_audit(result, prompt=prompt, ctx=ctx)
        return result

    def _create_memory(
        self, ctx, retrieved, validated, guarded, prompt,
        retrieval_latency_ms, llm_latency_ms, validator_latency_ms, guardrails_latency_ms,
    ):
        """Mint a MemoryAtom from the LLM's create_memory action.

        Bypasses the extraction worker (direct mint, exactly as
        VisionPipeline.capture does). kind chosen by the LLM (default
        "fact"); source_pipeline_version="instruction" so the atom is
        distinguishable from transcript / vision atoms.
        """
        action = validated.action
        if self._atom_store is None:
            result = self._build_result(
                ctx, retrieved, outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE, action=action,
                    refusal_reason=RejectionReason.UNKNOWN,
                    refusal_message="memory store is not configured on this server",
                ),
                retrieval_latency_ms=retrieval_latency_ms, llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms, guardrails_latency_ms=guardrails_latency_ms,
            )
            self._safe_audit(result, prompt=prompt, ctx=ctx)
            return result
        atom = MemoryAtom(
            atom_id=self._ids.new(),
            session_id=ctx.session_id or "",
            source_event_id="",  # direct mint — no source capture event
            kind=action.memory_kind or "fact",
            text=action.text,
            created_at=self._clock.now(),
            start_ms=0,
            occurred_at=self._clock.now(),
            source_pipeline_version="instruction",
        )
        self._atom_store.append(atom)
        result = self._build_result(
            ctx, retrieved, outcome=PlannerOutcome.CREATE_MEMORY, guarded=guarded,
            retrieval_latency_ms=retrieval_latency_ms, llm_latency_ms=llm_latency_ms,
            validator_latency_ms=validator_latency_ms, guardrails_latency_ms=guardrails_latency_ms,
            memory_atom_id=atom.atom_id,
        )
        self._safe_audit(result, prompt=prompt, ctx=ctx)
        return result

    def _create_reminder(
        self, ctx, retrieved, validated, guarded, prompt,
        retrieval_latency_ms, llm_latency_ms, validator_latency_ms, guardrails_latency_ms,
    ):
        """Mint a reminder: a kind='reminder' MemoryAtom (the text) + a
        ReminderStore schedule row (the due_at). occurred_at is the due_at
        so the reminder shows on the timeline at its fire time. The
        ReminderSweeper (Task 9) fires it as a proactive message when due.
        """
        action = validated.action
        if self._atom_store is None or self._reminder_store is None:
            result = self._build_result(
                ctx, retrieved, outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE, action=action,
                    refusal_reason=RejectionReason.UNKNOWN,
                    refusal_message="reminder store is not configured on this server",
                ),
                retrieval_latency_ms=retrieval_latency_ms, llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms, guardrails_latency_ms=guardrails_latency_ms,
            )
            self._safe_audit(result, prompt=prompt, ctx=ctx)
            return result
        due_at = action.due_at or self._clock.now()
        atom_id = self._ids.new()
        # Write the schedule row BEFORE the atom so a failure leaves no ghost
        # atom (a kind='reminder' atom with no schedule row would never fire
        # but still show on the /memory timeline — a silent partial failure).
        # ReminderStore.add keys on atom_id but does not reference AtomStore,
        # so schedule-first is feasible. AtomStore has no single-atom remove
        # method (only delete_range by start_ms), so reverse order is cleaner
        # than best-effort rollback of a just-appended atom. If add() raises,
        # REFUSE with a clear reason and no dangling atom. If the atom append
        # then fails, a schedule row with no atom remains — but the sweeper
        # fires from the schedule row's text, so the reminder still nudges the
        # user (less confusing than a ghost atom that never fires).
        try:
            self._reminder_store.add(
                atom_id=atom_id, session_id=ctx.session_id or "",
                text=action.text, due_at=due_at,
            )
        except Exception as exc:
            result = self._build_result(
                ctx, retrieved, outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE, action=action,
                    refusal_reason=RejectionReason.UNKNOWN,
                    refusal_message=f"failed to schedule reminder: {exc}",
                ),
                retrieval_latency_ms=retrieval_latency_ms, llm_latency_ms=llm_latency_ms,
                validator_latency_ms=validator_latency_ms, guardrails_latency_ms=guardrails_latency_ms,
            )
            self._safe_audit(result, prompt=prompt, ctx=ctx)
            return result
        atom = MemoryAtom(
            atom_id=atom_id,
            session_id=ctx.session_id or "",
            source_event_id="",
            kind="reminder",
            text=action.text,
            created_at=self._clock.now(),
            start_ms=0,
            occurred_at=due_at,
            source_pipeline_version="instruction",
        )
        self._atom_store.append(atom)
        result = self._build_result(
            ctx, retrieved, outcome=PlannerOutcome.CREATE_REMINDER, guarded=guarded,
            retrieval_latency_ms=retrieval_latency_ms, llm_latency_ms=llm_latency_ms,
            validator_latency_ms=validator_latency_ms, guardrails_latency_ms=guardrails_latency_ms,
            reminder_id=atom.atom_id,
        )
        self._safe_audit(result, prompt=prompt, ctx=ctx)
        return result

    def _do_retrieve(self, ctx: PlannerContext) -> RetrievedContext:
        # P3: the retriever only needs a string to embed. For
        # UserRequest the trigger text is the question; for Proactive
        # the v1 transcript is empty so the empty-string embedding
        # pulls from the session (the retriever handles that).
        query_text = (
            ctx.trigger.text if isinstance(ctx.trigger, UserRequest) else ctx.trigger.transcript
        )
        return self._retriever.retrieve(
            type("RC", (), {
                "query_text": query_text,
                "limit": ctx.limit,
                "session_id": ctx.session_id,
            })()
        )

    def _build_result(
        self,
        ctx: PlannerContext,
        retrieved: RetrievedContext,
        *,
        outcome: PlannerOutcome,
        guarded: GuardedAction,
        retrieval_latency_ms: int,
        llm_latency_ms: int,
        validator_latency_ms: int,
        guardrails_latency_ms: int,
        command_id: str | None = None,
        command_status: str | None = None,
        memory_atom_id: str | None = None, reminder_id: str | None = None,
    ) -> PlannerResult:
        # Map outcome -> confidence band (per the wire contract).
        if outcome == PlannerOutcome.RETURN:
            band = _band_for(guarded.action.confidence)
        elif outcome == PlannerOutcome.RETURN_WITH_UNCERTAINTY:
            band = _band_for(guarded.action.confidence)
        else:
            band = None
        # The mapper renders only the cited atoms. We carry the full
        # retrieved set so the mapper can resolve them by id; the
        # ``atom_ids`` field carries the cited ones.
        total_latency = retrieval_latency_ms + llm_latency_ms + validator_latency_ms + guardrails_latency_ms
        self._metrics.observe(Metrics.PLANNER_LATENCY_MS, total_latency)
        # Outcome distribution — the baseline any future planner backend
        # is compared against (spec Roadmap 0.3). Tagged, not name-suffixed:
        # the recorder rejects names outside KNOWN_METRICS.
        self._metrics.increment(
            Metrics.PLANNER_OUTCOME_TOTAL, {"outcome": outcome.value},
        )
        return PlannerResult(
            request_id=ctx.request_id,
            retrieval_trace_id=retrieved.retrieval_trace_id,
            audit_id=None,
            outcome=outcome,
            answer=guarded.action.text if outcome not in (PlannerOutcome.REFUSE, PlannerOutcome.ISSUE_COMMAND) else None,
            confidence=guarded.action.confidence if outcome != PlannerOutcome.REFUSE else None,
            confidence_band=band,
            atom_ids=guarded.action.atom_ids,
            refusal_reason=guarded.refusal_reason,
            refusal_message=guarded.refusal_message,
            command_id=command_id,
            command_status=command_status,
            memory_atom_id=memory_atom_id,
            reminder_id=reminder_id,
            retrieval_latency_ms=retrieval_latency_ms,
            llm_latency_ms=llm_latency_ms,
            validator_latency_ms=validator_latency_ms,
            guardrails_latency_ms=guardrails_latency_ms,
            total_latency_ms=total_latency,
            atoms=retrieved.atoms,
        )

    def _safe_audit(
        self,
        result: PlannerResult,
        prompt: Prompt | None,
        ctx: PlannerContext | None = None,
    ) -> None:
        try:
            entry = {
                "request_id": result.request_id,
                "retrieval_trace_id": result.retrieval_trace_id,
                "ts": self._clock.now().isoformat(),
                "outcome": result.outcome.value,
                "latency_ms": result.total_latency_ms,
            }
            # P3: stamp the trigger source so the audit log can be
            # filtered by user_request vs proactive. Default to
            # user_request when ctx is None (legacy / unit-test paths
            # that don't pass ctx — should not happen in production).
            if ctx is not None:
                entry["trigger_source"] = (
                    "proactive" if isinstance(ctx.trigger, Proactive) else "user_request"
                )
            if prompt is not None:
                entry["raw"] = prompt.user
                entry["prompt_hash"] = _hash_prompt(prompt)
            entry["validated"] = {"atom_ids": list(result.atom_ids), "confidence": result.confidence}
            entry["guarded"] = {"refusal_reason": str(result.refusal_reason) if result.refusal_reason else None}
            if result.command_id is not None:
                entry["command_id"] = result.command_id
                entry["command_status"] = result.command_status
            audit_id = self._audit.record(entry)
            # Mutate the immutable result: since PlannerResult is frozen,
            # we use object.__setattr__ to attach the audit_id (H3: this
            # is the only mutable field post-construction).
            object.__setattr__(result, "audit_id", audit_id)
            self._metrics.increment(Metrics.AUDIT_RECORDS_TOTAL, tags={"status": "ok"})
        except Exception:
            self._metrics.increment(Metrics.AUDIT_RECORDS_TOTAL, tags={"status": "failed"})


# --- helpers ----------------------------------------------------------------


def _band_for(confidence: float) -> str:
    if confidence >= 0.85:
        return "high"
    if confidence >= 0.60:
        return "medium"
    return "low"


def _hash_prompt(prompt: Prompt) -> str:
    """A short, stable hash of the prompt so audit can cite the shape."""
    import hashlib
    return hashlib.sha256((prompt.system + "\n" + prompt.user).encode("utf-8")).hexdigest()[:16]


def _default_ttl():
    """Default command TTL: 5 minutes. The dispatcher expires commands
    past this; the relay won't deliver stale commands."""
    from datetime import timedelta
    return timedelta(minutes=5)


def _map_validator_rejection(reason) -> RejectionReason:
    """Map the command validator's :class:`RejectionReason` to the
    generic :class:`RejectionReason` the Planner carries. Most map
    to ``SCHEMA_MISMATCH`` (param-related issues) or ``UNKNOWN``."""
    wire = reason.value
    if wire == "unknown_command_type":
        return RejectionReason.SCHEMA_MISMATCH
    if wire == "missing_required_param":
        return RejectionReason.SCHEMA_MISMATCH
    if wire == "invalid_param_type":
        return RejectionReason.SCHEMA_MISMATCH
    if wire == "param_out_of_range":
        return RejectionReason.SCHEMA_MISMATCH
    if wire == "unknown_param":
        return RejectionReason.SCHEMA_MISMATCH
    if wire == "invalid_idempotency_key":
        return RejectionReason.SCHEMA_MISMATCH
    return RejectionReason.UNKNOWN


def _map_guardrail_rejection(reason) -> RejectionReason:
    """Map the command guardrails' :class:`RejectionReason` to the
    generic enum the Planner carries. Capability + resource issues
    become ``NOT_AUTONOMOUS`` (the device can't act right now);
    confidence issues become ``CONFIDENCE_OUT_OF_RANGE``."""
    if reason is None:
        return RejectionReason.UNKNOWN
    wire = reason.value
    if wire == "capability_unavailable":
        return RejectionReason.NOT_AUTONOMOUS
    if wire == "resource_unavailable":
        return RejectionReason.NOT_AUTONOMOUS
    if wire == "command_confidence_too_low":
        return RejectionReason.CONFIDENCE_OUT_OF_RANGE
    return RejectionReason.UNKNOWN


def guarded_action_for_command(action: AgentAction, command_id: str) -> GuardedAction:
    """The Planner's result-building needs a GuardedAction even for
    the command path; this helper wraps the action in a synthetic
    RETURN-shape GuardedAction (the real outcome is ISSUE_COMMAND,
    but the GuardedAction is just a vehicle for the action + reason
    in the build path)."""
    return GuardedAction(
        outcome=GuardOutcome.RETURN,  # placeholder; outcome is on PlannerResult
        action=action,
        refusal_reason=None,
        refusal_message=None,
    )
