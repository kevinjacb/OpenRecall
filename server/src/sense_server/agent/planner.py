"""The real Planner (N3.2).

The Planner orchestrates the read path:

  1. RETRIEVE — call :class:`Retriever` for relevant atoms.
  2. SHORT-CIRCUIT — if no atoms, refuse with NO_SUPPORTING_MEMORY
     (don't bother the LLM with a guaranteed failure).
  3. BUILD CONTEXT — call :class:`ContextBuilder` with the trigger,
     retrieved atoms, and capabilities.
  4. REASON — call the async :class:`AgentLLM` (H4 — doesn't block).
  5. VALIDATE — strict JSON schema + provenance enforcement.
  6. GUARDRAIL — confidence-gated autonomy + rate limit.
  7. AUDIT — best-effort write to the durable log (H3 — failure does
     not lose the response).

The Planner is **stateless** (INV-1): every call takes the same
arguments and returns the same result (modulo the freshly-minted
request_id / trace_id / audit_id from the injected
:class:`~sense_server.contracts.id_generator.IdGenerator`). No
instance state, no rate-limit counters held on ``self`` — they live on
the :class:`Guardrails` seam, which the caller can swap.
"""
from __future__ import annotations

import time
from typing import Protocol, runtime_checkable

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.metrics import Metrics
from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    GuardOutcome,
    GuardedAction,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Prompt,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
    ValidatedAction,
    ValidatorContext,
)

_REFUSE = GuardOutcome.REFUSE


@runtime_checkable
class PlannerLike(Protocol):
    """The Planner's public surface — tests depend on this Protocol."""

    async def plan(self, ctx: PlannerContext) -> PlannerResult: ...


class Planner:
    """The read-path orchestrator. See module docstring."""

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

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        # 1. RETRIEVE
        t0 = time.monotonic()
        retrieved = self._do_retrieve(ctx)
        retrieval_latency = int((time.monotonic() - t0) * 1000)
        self._metrics.observe(Metrics.RETRIEVAL_LATENCY_MS, retrieval_latency)

        if not retrieved.atoms:
            # 2. SHORT-CIRCUIT — refuse without bothering the LLM
            result = self._build_result(
                ctx, retrieved,
                outcome=PlannerOutcome.REFUSE,
                guarded=GuardedAction(
                    outcome=_REFUSE,
                    action=AgentAction(
                        kind=AgentActionKind.NO_MEMORY,
                        text="",
                        atom_ids=(),
                        confidence=0.0,
                    ),
                    refusal_reason=RejectionReason.NO_SUPPORTING_MEMORY,
                    refusal_message="No relevant memory found.",
                ),
                retrieval_latency_ms=retrieval_latency,
                llm_latency_ms=0,
                validator_latency_ms=0,
                guardrails_latency_ms=0,
            )
            self._safe_audit(result, prompt=None)
            return result

        # 3. BUILD CONTEXT
        capabilities = self._caps.capabilities()
        prompt = self._context_builder.build(
            ctx.trigger_text, retrieved, capabilities
        )

        # 4. REASON (async — H4)
        t1 = time.monotonic()
        llm_result = await self._llm.reason(prompt)
        llm_latency = int((time.monotonic() - t1) * 1000)
        self._metrics.observe(Metrics.LLM_LATENCY_MS, llm_latency)

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

        # 7. BUILD RESULT
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
        self._safe_audit(result, prompt=prompt)
        return result

    def _do_retrieve(self, ctx: PlannerContext) -> RetrievedContext:
        return self._retriever.retrieve(
            type("RC", (), {
                "query_text": ctx.trigger_text,
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
        return PlannerResult(
            request_id=ctx.request_id,
            retrieval_trace_id=retrieved.retrieval_trace_id,
            audit_id=None,
            outcome=outcome,
            answer=guarded.action.text if outcome != PlannerOutcome.REFUSE else None,
            confidence=guarded.action.confidence if outcome != PlannerOutcome.REFUSE else None,
            confidence_band=band,
            atom_ids=guarded.action.atom_ids,
            refusal_reason=guarded.refusal_reason,
            refusal_message=guarded.refusal_message,
            retrieval_latency_ms=retrieval_latency_ms,
            llm_latency_ms=llm_latency_ms,
            validator_latency_ms=validator_latency_ms,
            guardrails_latency_ms=guardrails_latency_ms,
            total_latency_ms=total_latency,
            atoms=retrieved.atoms,
        )

    def _safe_audit(self, result: PlannerResult, prompt: Prompt | None) -> None:
        try:
            entry = {
                "request_id": result.request_id,
                "retrieval_trace_id": result.retrieval_trace_id,
                "ts": self._clock.now().isoformat(),
                "outcome": result.outcome.value,
                "latency_ms": result.total_latency_ms,
            }
            if prompt is not None:
                entry["raw"] = prompt.user
                entry["prompt_hash"] = _hash_prompt(prompt)
            entry["validated"] = {"atom_ids": list(result.atom_ids), "confidence": result.confidence}
            entry["guarded"] = {"refusal_reason": str(result.refusal_reason) if result.refusal_reason else None}
            audit_id = self._audit.record(entry)
            # Mutate the immutable result: since PlannerResult is frozen,
            # we use object.__setattr__ to attach the audit_id (H3: this
            # is the only mutable field post-construction).
            object.__setattr__(result, "audit_id", audit_id)
            self._metrics.increment(Metrics.AUDIT_RECORDS_TOTAL, tags={"status": "ok"})
        except Exception:
            self._metrics.increment(Metrics.AUDIT_RECORDS_TOTAL, tags={"status": "failed"})


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
