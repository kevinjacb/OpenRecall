"""P1 instruction-processor exit criteria (spec §1.6, sim-only).

Each utterance becomes the right outcome through the real planner
pipeline (strict validator + confidence guardrails + command
validator/guardrails/dispatcher + atom store + reminder store). The LLM
is scripted (the parser + validator + planner are the system under test),
which is the codebase's established test style (see test_planner_issue_command.py).
No hardware, no firmware.
"""
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
from openrecall_server.contracts.types import (
    AgentAction, AgentActionKind, IssueCommandPayload, LLMResult,
    PlannerContext, PlannerOutcome, RetrievedContext, UserRequest,
)
from openrecall_server.gateway.core import ProactiveOutbox
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.reminders.store import InMemoryReminderStore
from openrecall_server.reminders.sweeper import ReminderSweeper


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


class _FullCaps:
    def capabilities(self):
        from openrecall_server.contracts.types import CapabilitySet
        return CapabilitySet(camera=True, microphone=True, retrospective_buffer=True)

    def resources(self):
        from openrecall_server.contracts.types import DeviceResourceStatus
        return DeviceResourceStatus(
            battery_pct=1.0, storage_free_bytes=1 << 30, relay_connected=True,
        )


def _build(planner_llm, atom_store=None, reminder_store=None, recent=None):
    signer = CommandSigner.generate()
    return Planner(
        retriever=_EmptyRetriever(),
        context_builder=ContextBuilder(),
        llm=planner_llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_FullCaps(),
        clock=_CallableClock(),
        ids=DeterministicIdGenerator(),
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(),
        dispatcher=CommandDispatcher(signer, clock=_CallableClock()),
        atom_store=atom_store or InMemoryAtomStore(),
        reminder_store=reminder_store or InMemoryReminderStore(),
        recent_transcript=recent,
    )


def _ctx(text):
    return PlannerContext(
        request_id="r", trigger=UserRequest(request_id="r", text=text), session_id="s1",
    )


@pytest.mark.asyncio
async def test_record_audio_utterance_issues_command():
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND, confidence=0.95,
        command=IssueCommandPayload(
            command_type="record_audio", params={"duration_s": 20},
            idempotency_key="record_audio_20s", confidence=0.95,
        ),
    )
    planner = _build(_ScriptedLLM(parsed))
    result = await planner.plan(_ctx("record my audio for the next 20 seconds"))
    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert result.command_status == "PENDING"
    issued = planner._dispatcher.pending()[0].command
    assert issued.type == "record_audio"
    assert issued.params == {"duration_s": 20}


@pytest.mark.asyncio
async def test_make_a_memory_utterance_mints_atom_from_recent_transcript():
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_MEMORY, text="The user likes espresso",
        memory_kind="preference", confidence=0.9,
    )

    class _Recent:
        def recent(self, session_id, max_age_s=60):
            return "user: I really like espresso in the morning"

    atom_store = InMemoryAtomStore()
    planner = _build(_ScriptedLLM(parsed), atom_store=atom_store, recent=_Recent())
    result = await planner.plan(_ctx("make a memory out of that"))
    assert result.outcome == PlannerOutcome.CREATE_MEMORY
    assert result.memory_atom_id is not None
    assert atom_store.atoms("s1")[0].text == "The user likes espresso"


@pytest.mark.asyncio
async def test_remind_me_utterance_mints_reminder_and_sweeper_fires():
    due = datetime(2026, 8, 18, 18, 0, tzinfo=timezone.utc)
    parsed = AgentAction(
        kind=AgentActionKind.CREATE_REMINDER, text="Call mom", due_at=due, confidence=0.9,
    )
    atom_store = InMemoryAtomStore()
    reminder_store = InMemoryReminderStore()
    outbox = ProactiveOutbox()
    planner = _build(
        _ScriptedLLM(parsed), atom_store=atom_store, reminder_store=reminder_store,
    )
    result = await planner.plan(_ctx("remind me to call mom at 6pm"))
    assert result.outcome == PlannerOutcome.CREATE_REMINDER
    assert result.reminder_id is not None
    # Before due: nothing fires.
    sweeper = ReminderSweeper(store=reminder_store, outbox=outbox, clock=None)
    assert sweeper.sweep_once(now=datetime(2026, 8, 18, 17, 0, tzinfo=timezone.utc)) == 0
    # At/after due: the reminder fires into the outbox.
    assert sweeper.sweep_once(now=due) == 1
    msgs = outbox.drain("s1")
    assert len(msgs) == 1
    assert "Call mom" in msgs[0].text


@pytest.mark.asyncio
async def test_start_video_utterance_issues_command():
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND, confidence=0.95,
        command=IssueCommandPayload(
            command_type="start_video", params={},
            idempotency_key="start_video", confidence=0.95,
        ),
    )
    planner = _build(_ScriptedLLM(parsed))
    result = await planner.plan(_ctx("start recording video"))
    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert planner._dispatcher.pending()[0].command.type == "start_video"


@pytest.mark.asyncio
async def test_stop_video_utterance_issues_command():
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND, confidence=0.95,
        command=IssueCommandPayload(
            command_type="stop_video", params={},
            idempotency_key="stop_video", confidence=0.95,
        ),
    )
    planner = _build(_ScriptedLLM(parsed))
    result = await planner.plan(_ctx("stop recording"))
    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert planner._dispatcher.pending()[0].command.type == "stop_video"