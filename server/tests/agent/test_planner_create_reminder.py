from datetime import datetime, timezone

import pytest

from openrecall_server.agent.audit import InMemoryAuditLogger
from openrecall_server.agent.context import ContextBuilder
from openrecall_server.agent.guardrails import ConfidenceGateGuardrails
from openrecall_server.agent.intent import AgentLLM
from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.agent.planner import Planner
from openrecall_server.agent.validator import StrictJSONValidator
from openrecall_server.contracts.clock import FakeClock as _BaseFakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.contracts.types import (
    AgentAction, AgentActionKind, LLMResult, PlannerContext, PlannerOutcome,
    RetrievedContext, UserRequest,
)
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.reminders.store import InMemoryReminderStore


class _CallableClock(_BaseFakeClock):
    def __call__(self):
        return self.now()


class _ScriptedLLM(AgentLLM):
    def __init__(self, parsed):
        self._parsed = parsed

    async def reason(self, prompt):
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


class _EmptyRetriever:
    def retrieve(self, *a, **kw):
        return RetrievedContext(atoms=(), retrieval_trace_id="t")


class _NoCaps:
    def capabilities(self):
        from openrecall_server.contracts.types import CapabilitySet
        return CapabilitySet()

    def resources(self):
        from openrecall_server.contracts.types import DeviceResourceStatus
        return DeviceResourceStatus()


def _ctx():
    return PlannerContext(
        request_id="r", trigger=UserRequest(request_id="r", text="remind me to call mom at 6pm"),
        session_id="s1",
    )


def _build(planner_llm, atom_store, reminder_store):
    return Planner(
        retriever=_EmptyRetriever(),
        context_builder=ContextBuilder(),
        llm=planner_llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_NoCaps(),
        clock=_CallableClock(),
        ids=DeterministicIdGenerator(),
        atom_store=atom_store,
        reminder_store=reminder_store,
    )


@pytest.mark.asyncio
async def test_create_reminder_mints_atom_and_schedule_row():
    due = datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc)
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_REMINDER, text="Call mom", due_at=due, confidence=0.9,
    )
    atom_store = InMemoryAtomStore()
    reminder_store = InMemoryReminderStore()
    planner = _build(_ScriptedLLM(parsed), atom_store, reminder_store)
    result = await planner.plan(_ctx())

    assert result.outcome == PlannerOutcome.CREATE_REMINDER
    assert result.reminder_id is not None
    atoms = atom_store.atoms("s1")
    assert len(atoms) == 1
    assert atoms[0].kind == "reminder"
    assert atoms[0].text == "Call mom"
    assert atoms[0].occurred_at == due
    row = reminder_store.get(result.reminder_id)
    assert row is not None
    assert row.due_at == due
    assert row.status == "pending"
    assert row.text == "Call mom"


@pytest.mark.asyncio
async def test_create_reminder_low_confidence_refused_and_mints_nothing():
    due = datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc)
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_REMINDER, text="Call mom", due_at=due, confidence=0.2,
    )
    atom_store = InMemoryAtomStore()
    reminder_store = InMemoryReminderStore()
    planner = _build(_ScriptedLLM(parsed), atom_store, reminder_store)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert atom_store.atoms("s1") == []
    assert reminder_store.list() == []