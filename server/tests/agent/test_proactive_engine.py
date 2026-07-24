"""Tests for the ProactiveTriggerEngine (P3).

The engine is a listener on the extraction worker. On every
SessionCompletion it builds a Proactive PlannerContext, calls the
planner, and forwards RETURN results to a WsSender. The engine is
best-effort: a 2-second timeout, all exceptions caught, every drop
counted. A proactive call that would ISSUE_COMMAND is refused by
the planner itself (Commit 2) — the engine never sees that case
unless the planner is mis-wired, in which case the engine drops
it as 'refused'.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.metrics import Metrics
from sense_server.contracts.types import (
    CapabilitySet,
    DeviceResourceStatus,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Proactive,
    RetrievedContext,
    ScoredAtom,
)
from sense_server.agent.proactive import ProactiveTriggerEngine, WsSender
from sense_server.memory.extraction_worker import SessionCompletion


def _retrieved():
    a = ScoredAtom(
        atom_id="a1", session_id="s1", kind="fact", text="hello",
        score=0.9,
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        start_ms=0,
    )
    return RetrievedContext(atoms=(a,), retrieval_trace_id="t1")


class _FakeWsSender:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []

    async def send_proactive(self, *, session_id, request_id, text, atoms):
        self.sent.append({
            "session_id": session_id,
            "request_id": request_id,
            "text": text,
            "atoms": atoms,
        })


class _PlannerStub:
    """A planner stub that records calls and returns a fixed result."""
    def __init__(self, result: PlannerResult):
        self._result = result
        self.calls: list[PlannerContext] = []

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        self.calls.append(ctx)
        return self._result


def _completion(session_id: str = "s1") -> SessionCompletion:
    return SessionCompletion(
        session_id=session_id,
        completed_at=datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        event_id_range=(0, 7),
    )


def _returned_result() -> PlannerResult:
    return PlannerResult(
        request_id="r1",
        retrieval_trace_id="t1",
        outcome=PlannerOutcome.RETURN,
        answer="here is what I found",
        confidence=0.9,
        atom_ids=("a1",),
    )


def _refused_result() -> PlannerResult:
    return PlannerResult(
        request_id="r1",
        retrieval_trace_id="t1",
        outcome=PlannerOutcome.REFUSE,
    )


@pytest.mark.asyncio
async def test_engine_builds_proactive_context():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_returned_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s42"))
    assert len(planner.calls) == 1
    ctx = planner.calls[0]
    assert isinstance(ctx.trigger, Proactive)
    assert ctx.session_id == "s42"
    assert ctx.limit == 10
    # The proactive context carries an event_id derived from the seq.
    assert ctx.trigger.event_id == "event_seq_7"


@pytest.mark.asyncio
async def test_engine_forwards_return_to_ws_sender():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_returned_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s1"))
    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent["session_id"] == "s1"
    assert sent["text"] == "here is what I found"
    assert sent["atoms"] == ("a1",)
    total = sum(
        v for (n, _), v in metrics._counters.items()
        if n == Metrics.PROACTIVE_DELIVERED_TOTAL
    )
    assert total == 1


@pytest.mark.asyncio
async def test_engine_drops_refuse():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_refused_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    total = sum(
        v for (n, _), v in metrics._counters.items()
        if n == Metrics.PROACTIVE_REFUSED_TOTAL
    )
    assert total == 1


@pytest.mark.asyncio
async def test_engine_times_out_and_counts_failure():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()

    class SlowPlanner:
        async def plan(self, ctx: PlannerContext) -> PlannerResult:
            await asyncio.sleep(10)
            return _returned_result()

    engine = ProactiveTriggerEngine(
        planner=SlowPlanner(), ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=0.05,
    )
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    total = sum(
        v for (n, _), v in metrics._counters.items()
        if n == Metrics.PROACTIVE_PLAN_FAILURE_TOTAL
    )
    assert total == 1


@pytest.mark.asyncio
async def test_engine_swallows_planner_exception():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()

    class BoomPlanner:
        async def plan(self, ctx: PlannerContext) -> PlannerResult:
            raise RuntimeError("planner crashed")

    engine = ProactiveTriggerEngine(
        planner=BoomPlanner(), ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    # The engine must not re-raise.
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    total = sum(
        v for (n, _), v in metrics._counters.items()
        if n == Metrics.PROACTIVE_PLAN_FAILURE_TOTAL
    )
    assert total == 1


def test_ws_sender_is_runtime_checkable():
    """The WsSender Protocol is runtime-checkable; the gateway's
    implementation in Commit 5 will satisfy it without explicit
    inheritance. This test pins the Protocol's surface."""

    class HasSend:
        async def send_proactive(self, *, session_id, request_id, text, atoms):
            pass

    class MissingSend:
        pass

    assert isinstance(HasSend(), WsSender)
    assert not isinstance(MissingSend(), WsSender)


@pytest.mark.asyncio
async def test_engine_set_ws_sender_swaps_target():
    """set_ws_sender lets run_gateway.py wire the per-connection
    GatewayCore into the engine after construction."""
    metrics = InMemoryMetricsRecorder()
    planner = _PlannerStub(_returned_result())
    sender_a = _FakeWsSender()
    sender_b = _FakeWsSender()
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender_a, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    engine.set_ws_sender(sender_b)

    await engine.on_session_completion(_completion("s1"))
    assert sender_a.sent == []
    assert len(sender_b.sent) == 1


# --- plan timeout config ----------------------------------------------------


def test_plan_timeout_from_env_defaults_to_8s():
    from sense_server.agent.proactive import (
        plan_timeout_from_env, DEFAULT_PLAN_TIMEOUT_S,
    )
    assert DEFAULT_PLAN_TIMEOUT_S == 8.0
    assert plan_timeout_from_env({}) == 8.0


def test_plan_timeout_from_env_reads_override():
    from sense_server.agent.proactive import plan_timeout_from_env
    assert plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "5.0"}) == 5.0


def test_plan_timeout_from_env_rejects_nonpositive_and_nonnumeric():
    from sense_server.agent.proactive import plan_timeout_from_env
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "0"})
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "-1"})
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "notanum"})
