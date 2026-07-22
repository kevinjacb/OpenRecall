"""Tests for the real Planner (N3.2 / INV-1, H3)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

import pytest

from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.metrics import Metrics
from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    GuardedAction,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Prompt,
    RejectionReason,
    RetrievedContext,
    RetrieverContext,
    ScoredAtom,
    UserRequest,
    ValidatedAction,
    ValidatorContext,
)
from sense_server.agent.audit import InMemoryAuditLogger
from sense_server.agent.guardrails import ConfidenceGateGuardrails
from sense_server.agent.intent import AgentLLM
from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.agent.planner import Planner
from sense_server.agent.validator import StrictJSONValidator


# --- fakes ------------------------------------------------------------------


def _atom(atom_id: str, text: str, score: float = 0.9) -> ScoredAtom:
    return ScoredAtom(
        atom_id=atom_id,
        session_id="s1",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=score,
    )


class FakeRetriever:
    def __init__(self, atoms: Iterable[ScoredAtom] = ()) -> None:
        self._atoms = list(atoms)
        self._clock = FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc))

    def retrieve(self, ctx: RetrieverContext) -> RetrievedContext:
        return RetrievedContext(
            atoms=tuple(self._atoms),
            retrieval_trace_id="trace-retrieval",
            top_score=self._atoms[0].score if self._atoms else float("-inf"),
            lowest_score=self._atoms[-1].score if self._atoms else float("-inf"),
            returned_count=len(self._atoms),
        )


class FakeAgentLLM:
    def __init__(self, parsed: AgentAction | None = None, parse_error: str | None = None) -> None:
        self._parsed = parsed
        self._parse_error = parse_error
        self.calls: list[Prompt] = []

    async def reason(self, prompt: Prompt) -> LLMResult:
        self.calls.append(prompt)
        if self._parse_error is not None:
            return LLMResult(raw="", parsed=None, parse_error=self._parse_error)
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


def _ctx(extra: dict | None = None) -> PlannerContext:
    # P3: use the new Trigger envelope (UserRequest).
    base = dict(
        request_id="req-1",
        trigger=UserRequest(request_id="req-1", text="hello"),
        session_id="s1",
    )
    if extra:
        base.update(extra)
    return PlannerContext(**base)


def _planner(retriever, llm, validator=None, guardrails=None, audit=None, metrics=None) -> Planner:
    return Planner(
        retriever=retriever,
        context_builder=__import__("sense_server.agent.context", fromlist=["ContextBuilder"]).ContextBuilder(),
        llm=llm,
        validator=validator or StrictJSONValidator(),
        guardrails=guardrails or ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=audit or InMemoryAuditLogger(),
        metrics=metrics or InMemoryMetricsRecorder(),
        capability_provider=__import__("sense_server.agent.capability", fromlist=["ConstantCapabilityProvider"]).ConstantCapabilityProvider(),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )


@pytest.mark.asyncio
async def test_planner_returns_answer_with_cited_atoms():
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)
    llm = FakeAgentLLM(parsed=parsed)
    p = _planner(FakeRetriever(atoms), llm)
    result = await p.plan(_ctx())
    assert result.outcome == PlannerOutcome.RETURN
    assert result.answer == "x"
    assert result.atom_ids == ("a1",)
    assert result.confidence == 0.9


@pytest.mark.asyncio
async def test_planner_refuses_when_no_retrieved_atoms():
    """Empty retrieval is refused with NO_SUPPORTING_MEMORY, with the
    no-memory direction owned by the v2 prompt + answer guardrails
    rather than a planner-side short-circuit (which was removed
    in batch-1 to make room for direct device-action admission on
    a cold index)."""
    # The LLM is now called on empty retrieval. The v2 prompt
    # tells it to return no_memory for factual questions, which
    # the answer guardrails then map to NO_SUPPORTING_MEMORY.
    llm = FakeAgentLLM(
        parsed=AgentAction(
            kind=AgentActionKind.NO_MEMORY,
            text="",
            atom_ids=(),
            confidence=0.5,
        )
    )
    p = _planner(FakeRetriever(atoms=()), llm)
    result = await p.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason == RejectionReason.NO_SUPPORTING_MEMORY
    # The LLM IS called when there are no supporting atoms; the
    # factual-question safety lives at the prompt + guardrails
    # level, not in a planner-side short-circuit.
    assert len(llm.calls) == 1


@pytest.mark.asyncio
async def test_planner_refuses_on_validator_rejection():
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1", "a_evil"), confidence=0.9)
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed))
    result = await p.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE


@pytest.mark.asyncio
async def test_planner_refuses_on_llm_parse_error():
    atoms = (_atom("a1", "hello"),)
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parse_error="bad json"))
    result = await p.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE


@pytest.mark.asyncio
async def test_planner_return_with_uncertainty_for_medium_confidence():
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.7)
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed))
    result = await p.plan(_ctx())
    assert result.outcome == PlannerOutcome.RETURN_WITH_UNCERTAINTY


@pytest.mark.asyncio
async def test_planner_audit_failure_does_not_lose_response_h3():
    """H3: a failed audit write must not lose the user's response."""
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)

    class FailingAudit:
        def record(self, entry):
            raise RuntimeError("disk full")

    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed), audit=FailingAudit())
    result = await p.plan(_ctx())
    # The response is still a valid answer.
    assert result.outcome == PlannerOutcome.RETURN
    assert result.answer == "x"


@pytest.mark.asyncio
async def test_planner_records_metrics():
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)
    metrics = InMemoryMetricsRecorder()
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed), metrics=metrics)
    await p.plan(_ctx())
    assert metrics.histogram(Metrics.PLANNER_LATENCY_MS).count == 1
    assert metrics.histogram(Metrics.RETRIEVAL_LATENCY_MS).count == 1


@pytest.mark.asyncio
async def test_planner_is_stateless_across_calls():
    """INV-1: the planner is stateless; two identical calls produce identical
    content (answer, confidence, atom_ids), with request_id and trace_id
    carried through from the input, and a fresh audit_id minted per call."""
    atoms = (_atom("a1", "hello"),)
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed))
    r1 = await p.plan(_ctx())
    r2 = await p.plan(_ctx())
    assert r1.answer == r2.answer
    assert r1.confidence == r2.confidence
    assert r1.atom_ids == r2.atom_ids
    # request_id is the one the caller passed; trace_id is from the
    # retriever. Both are stable across calls with the same inputs.
    assert r1.request_id == r2.request_id == "req-1"
    # audit_id is unique per call (it's a counter).
    assert r1.audit_id != r2.audit_id


@pytest.mark.asyncio
async def test_planner_includes_retrieved_atoms_in_result():
    atoms = (_atom("a1", "hello"), _atom("a2", "world"))
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)
    p = _planner(FakeRetriever(atoms), FakeAgentLLM(parsed=parsed))
    result = await p.plan(_ctx())
    assert {a.atom_id for a in result.atoms} == {"a1", "a2"}
