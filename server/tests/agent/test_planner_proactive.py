"""Tests for the proactive ISSUE_COMMAND prohibition inside Planner.plan (P3).

The proactive trigger is FORBIDDEN from issuing device commands: a
proactive call is initiated by the server, not the user, and a
hallucination on the proactive path would become a real device
action. The planner enforces this as a type-system guarantee — the
check happens before CommandValidator is even consulted. The audit
log records trigger_source="proactive" for every proactive outcome.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openrecall_server.agent.audit import InMemoryAuditLogger
from openrecall_server.agent.context import ContextBuilder
from openrecall_server.agent.guardrails import ConfidenceGateGuardrails
from openrecall_server.agent.guardrails_command import StrictCommandGuardrails
from openrecall_server.agent.intent import AgentLLM
from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.agent.planner import Planner
from openrecall_server.agent.validator import StrictJSONValidator
from openrecall_server.agent.validator_command import StrictCommandValidator
from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.contracts.clock import FakeClock as _BaseFakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator


class _CallableClock(_BaseFakeClock):
    """FakeClock that is also callable — the dispatcher's pending()
    path uses self._clock() (legacy), so tests need a clock that
    supports both .now() and __call__()."""
    def __call__(self) -> datetime:
        return self.now()
from openrecall_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    CapabilitySet,
    DeviceResourceStatus,
    IssueCommandPayload,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    Proactive,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
    UserRequest,
)


def _retrieved() -> RetrievedContext:
    a = ScoredAtom(
        atom_id="a1",
        session_id="s1",
        kind="fact",
        text="hello",
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
        start_ms=0,
        score=0.9,
    )
    return RetrievedContext(
        atoms=(a,),
        retrieval_strategy="sim_recency",
        scorer_version="v1",
        index_name="in_memory",
        index_version="v1",
        top_score=0.9,
        lowest_score=0.9,
        returned_count=1,
        candidate_count=1,
        retrieval_latency_ms=10,
        retrieval_trace_id="t1",
    )


class _LLM:
    def __init__(self, parsed):
        self._parsed = parsed

    async def reason(self, prompt):
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


class _Retriever:
    def retrieve(self, *a, **kw):
        return _retrieved()


class _Caps:
    def capabilities(self):
        return CapabilitySet(
            camera=True,
            microphone=True,
            retrospective_buffer=True,
            display=False,
            speaker=False,
        )

    def resources(self):
        return DeviceResourceStatus(
            battery_pct=1.0,
            storage_free_bytes=1 << 30,
            camera_available=True,
            microphone_available=True,
            recording=False,
            relay_connected=True,
        )


def _command_action() -> AgentAction:
    return AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=("a1",),
        confidence=0.95,
        command=IssueCommandPayload(
            command_type="record_video",
            params={"duration_s": 10},
            idempotency_key="proactive-1",
            confidence=0.95,
        ),
    )


def _build_planner(audit, dispatcher) -> Planner:
    return Planner(
        retriever=_Retriever(),
        context_builder=ContextBuilder(),
        llm=_LLM(parsed=_command_action()),
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=audit,
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_Caps(),
        clock=_CallableClock(datetime(2026, 7, 19, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(),
        dispatcher=dispatcher,
    )


@pytest.mark.asyncio
async def test_proactive_trigger_refuses_issue_command():
    """Planner.plan refuses ISSUE_COMMAND when trigger is Proactive.

    The dispatcher is wired (so the planner *could* issue), but the
    proactive restriction must short-circuit before the command path
    runs at all.
    """
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_CallableClock(datetime(2026, 7, 19, tzinfo=timezone.utc)))
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=Proactive(request_id="r1", event_id="s1:7", transcript=""),
        session_id="s1",
    )
    result = await planner.plan(ctx)

    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason == RejectionReason.PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND
    # The dispatcher's pending list is empty — the command was never issued.
    assert dispatcher.pending() == []


@pytest.mark.asyncio
async def test_proactive_trigger_audit_records_trigger_source():
    """Audit entries for proactive outcomes record trigger_source='proactive'."""
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_CallableClock(datetime(2026, 7, 19, tzinfo=timezone.utc)))
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=Proactive(request_id="r1", event_id="s1:7", transcript=""),
        session_id="s1",
    )
    await planner.plan(ctx)

    # At least one entry should carry the proactive source.
    proactive_entries = [e for e in audit.entries if e.get("trigger_source") == "proactive"]
    assert len(proactive_entries) >= 1


@pytest.mark.asyncio
async def test_user_request_can_still_issue_command():
    """Regression: a UserRequest trigger must still ISSUE_COMMAND successfully."""
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_CallableClock(datetime(2026, 7, 19, tzinfo=timezone.utc)))
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=UserRequest(request_id="r1", text="record a 10s video"),
        session_id="s1",
    )
    result = await planner.plan(ctx)

    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert result.command_id is not None
    # UserRequest entries carry trigger_source='user_request'.
    user_entries = [e for e in audit.entries if e.get("trigger_source") == "user_request"]
    assert any(user_entries)
