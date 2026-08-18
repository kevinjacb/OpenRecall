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


def _ctx():
    return PlannerContext(
        request_id="r", trigger=UserRequest(request_id="r", text="remember that I like espresso"),
        session_id="s1",
    )


def _build(planner_llm, atom_store, recent=None):
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
        recent_transcript=recent,
    )


class _NoCaps:
    def capabilities(self):
        from openrecall_server.contracts.types import CapabilitySet
        return CapabilitySet()

    def resources(self):
        from openrecall_server.contracts.types import DeviceResourceStatus
        return DeviceResourceStatus()


@pytest.mark.asyncio
async def test_create_memory_mints_atom_and_returns_memory_atom_id():
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_MEMORY,
        text="The user likes espresso",
        memory_kind="preference",
        confidence=0.9,
    )
    atom_store = InMemoryAtomStore()
    planner = _build(_ScriptedLLM(parsed), atom_store)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.CREATE_MEMORY
    assert result.memory_atom_id is not None
    atoms = atom_store.atoms("s1")
    assert len(atoms) == 1
    assert atoms[0].kind == "preference"
    assert atoms[0].text == "The user likes espresso"
    assert atoms[0].source_pipeline_version == "instruction"


@pytest.mark.asyncio
async def test_create_memory_defaults_kind_to_fact():
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_MEMORY, text="a fact", confidence=0.9,
    )
    atom_store = InMemoryAtomStore()
    planner = _build(_ScriptedLLM(parsed), atom_store)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.CREATE_MEMORY
    assert atom_store.atoms("s1")[0].kind == "fact"


@pytest.mark.asyncio
async def test_create_memory_low_confidence_is_refused_and_mints_nothing():
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_MEMORY, text="x", confidence=0.2,
    )
    atom_store = InMemoryAtomStore()
    planner = _build(_ScriptedLLM(parsed), atom_store)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert atom_store.atoms("s1") == []


@pytest.mark.asyncio
async def test_create_memory_passes_recent_transcript_into_context():
    seen = {}

    class _CaptureLLM(AgentLLM):
        async def reason(self, prompt):
            seen["system"] = prompt.system
            return LLMResult(raw="", parsed=AgentAction(
                kind=AgentActionKind.CREATE_MEMORY, text="x", confidence=0.9,
            ), parse_error=None)

    class _Recent:
        def recent(self, session_id, max_age_s=60):
            return "the user just said they like espresso"

    planner = _build(_CaptureLLM(), InMemoryAtomStore(), recent=_Recent())
    await planner.plan(_ctx())
    assert "the user just said they like espresso" in seen["system"]