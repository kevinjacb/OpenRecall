"""Tests for the P2-commands Planner integration.

The Planner recognizes ``kind: "issue_command"`` in the LLM's
parsed action and runs the command through:

  1. CommandValidator — schema + allowlist + param bounds
  2. CommandGuardrails — capability + resource + confidence
  3. CommandDispatcher.issue — sign + track + idempotency dedup

The result is :class:`PlannerOutcome.ISSUE_COMMAND` with the
``command_id`` and the initial lifecycle status (``PENDING``).

A failure in any of those three layers becomes a refusal with a
user-facing message; the audit log carries the rejection reason.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sense_server.agent.audit import InMemoryAuditLogger
from sense_server.agent.capability import ConstantCapabilityProvider
from sense_server.agent.context import ContextBuilder
from sense_server.agent.guardrails import ConfidenceGateGuardrails
from sense_server.agent.guardrails_command import StrictCommandGuardrails
from sense_server.agent.intent import OpenAICompatibleAgentLLM, AgentLLM
from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.agent.planner import Planner
from sense_server.agent.validator import StrictJSONValidator
from sense_server.agent.validator_command import StrictCommandValidator
from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.model import Command
from sense_server.commands.signing import CommandSigner
from sense_server.commands.status import CommandStatus
from sense_server.contracts.clock import FakeClock as _BaseFakeClock


class _CallableClock(_BaseFakeClock):
    """FakeClock that is also callable — the dispatcher's pending()
    path uses self._clock() (legacy), so tests need a clock that
    supports both .now() and __call__()."""
    def __call__(self) -> datetime:
        return self.now()
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    IssueCommandPayload,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
    UserRequest,
)


# --- fakes ------------------------------------------------------------------


def _atom(atom_id: str = "a1") -> ScoredAtom:
    return ScoredAtom(
        atom_id=atom_id,
        session_id="s1",
        kind="fact",
        text="x",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=0.9,
    )


def _retrieved() -> RetrievedContext:
    return RetrievedContext(
        atoms=(_atom(),),
        retrieval_strategy="sim_recency",
        scorer_version="v1",
        index_name="in_memory",
        index_version="v1",
        top_score=0.9,
        lowest_score=0.9,
        returned_count=1,
        candidate_count=1,
        retrieval_latency_ms=10,
        retrieval_trace_id="trace-1",
    )


class FakeAgentLLM(AgentLLM):
    """A fake LLM that returns a scripted :class:`LLMResult`."""

    def __init__(self, parsed: AgentAction | None = None, parse_error: str | None = None) -> None:
        self._parsed = parsed
        self._parse_error = parse_error
        self.calls: list = []

    async def reason(self, prompt) -> LLMResult:
        self.calls.append(prompt)
        if self._parse_error is not None:
            return LLMResult(raw="", parsed=None, parse_error=self._parse_error)
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


def _ctx() -> PlannerContext:
    # P3: use the new Trigger envelope (UserRequest).
    return PlannerContext(
        request_id="req-1",
        trigger=UserRequest(request_id="req-1", text="record the next 10s"),
        session_id="s1",
    )


def _build_planner(
    llm: AgentLLM,
    command_validator=...,
    command_guardrails=...,
    dispatcher: CommandDispatcher | None = None,
) -> Planner:
    """Build a Planner with all the command-path components wired in.

    command_validator and command_guardrails default to the strict
    implementations; the dispatcher defaults to an in-memory one.
    """
    if command_validator is ...:
        command_validator = StrictCommandValidator()
    if command_guardrails is ...:
        command_guardrails = StrictCommandGuardrails()
    if dispatcher is None:
        signer = CommandSigner.generate()
        dispatcher = CommandDispatcher(signer, clock=_CallableClock())
    return Planner(
        retriever=_MockRetriever(),
        context_builder=ContextBuilder(),
        llm=llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_MockCapabilityProvider(),
        clock=_CallableClock(),
        ids=DeterministicIdGenerator(),
        command_validator=command_validator,
        command_guardrails=command_guardrails,
        dispatcher=dispatcher,
    )


class _MockRetriever:
    """A retriever that returns a fixed RetrievedContext for any ctx."""

    def retrieve(self, *args, **kwargs) -> RetrievedContext:
        return _retrieved()


class _MockCapabilityProvider:
    """A capability provider that always returns the full set."""

    def capabilities(self):
        from sense_server.contracts.types import CapabilitySet
        return CapabilitySet(camera=True, microphone=True, retrospective_buffer=True)

    def resources(self):
        from sense_server.contracts.types import DeviceResourceStatus
        return DeviceResourceStatus(
            battery_pct=1.0,
            storage_free_bytes=1 << 30,
            camera_available=True,
            microphone_available=True,
            recording=False,
            relay_connected=True,
        )


# --- happy path: ISSUE_COMMAND issued successfully ----------------------


@pytest.mark.asyncio
async def test_issue_command_happy_path_dispatches_and_returns_command_id():
    """LLM says: record the next 10 seconds. Validator passes,
    guardrails pass, dispatcher issues, result is ISSUE_COMMAND
    with the new command_id and PENDING status.
    """
    payload = IssueCommandPayload(
        command_type="record_video",
        params={"duration_s": 10},
        idempotency_key="user-1-record-10s",
        confidence=0.95,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=("a1",),
        confidence=0.95,
        command=payload,
    )
    llm = FakeAgentLLM(parsed=parsed)
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_CallableClock())
    planner = _build_planner(llm, dispatcher=dispatcher)

    result = await planner.plan(_ctx())

    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert result.command_id is not None
    # command_id format depends on the id generator; just check it's set.
    # The dispatcher's record shows the command at PENDING with our params.
    assert result.command_status == "PENDING"
    assert dispatcher.get_status(result.command_id) == CommandStatus.PENDING
    assert dispatcher.get_history(result.command_id)[-1].to_status == CommandStatus.PENDING


@pytest.mark.asyncio
async def test_issue_command_record_uses_payload_params():
    """The Command dispatched to the device uses the params the LLM
    passed. The wire format is the LLM's intent, not a server-side
    transformation (the validator has already cleaned them)."""
    payload = IssueCommandPayload(
        command_type="record_video",
        params={"duration_s": 25},
        idempotency_key="user-1-record-25s",
        confidence=0.9,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=0.9,
        command=payload,
    )
    llm = FakeAgentLLM(parsed=parsed)
    dispatcher = CommandDispatcher(CommandSigner.generate(), clock=_CallableClock())
    planner = _build_planner(llm, dispatcher=dispatcher)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    # Verify the actual signed command has our params.
    issued = dispatcher.pending()[0].command
    assert issued.type == "record_video"
    assert issued.params == {"duration_s": 25}


# --- validator rejection ----------------------------------------------------


@pytest.mark.asyncio
async def test_issue_command_with_invalid_params_becomes_refuse():
    """LLM says: record for 999999 seconds. The validator rejects
    (out of range), the result is REFUSE with the validator's
    rejection reason."""
    payload = IssueCommandPayload(
        command_type="record_video",
        params={"duration_s": 999999},  # out of [1, 30]
        idempotency_key="user-1-huge",
        confidence=0.9,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=0.9,
        command=payload,
    )
    llm = FakeAgentLLM(parsed=parsed)
    planner = _build_planner(llm)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    # The validator's rejection reason propagates; PLAN_ANSWER has
    # distinct codes for INVALID_JSON vs SCHEMA_MISMATCH vs PARAM_OUT_OF_RANGE.
    # The validator_command module defines its own rejection reason
    # enum; the Planner wraps it in the generic RejectionReason set.
    assert result.refusal_reason is not None


@pytest.mark.asyncio
async def test_issue_command_without_camera_capability_becomes_refuse():
    """The device doesn't advertise a camera. capture_photo is
    refused by the guardrails.
    """
    payload = IssueCommandPayload(
        command_type="capture_photo",
        params={},
        idempotency_key="user-1-photo",
        confidence=0.9,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=0.9,
        command=payload,
    )

    class _NoCamCaps:
        def capabilities(self):
            from sense_server.contracts.types import CapabilitySet
            return CapabilitySet(camera=False, microphone=True, retrospective_buffer=True)
        def resources(self):
            from sense_server.contracts.types import DeviceResourceStatus
            return DeviceResourceStatus()

    llm = FakeAgentLLM(parsed=parsed)
    planner = Planner(
        retriever=_MockRetriever(),
        context_builder=ContextBuilder(),
        llm=llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_NoCamCaps(),
        clock=_CallableClock(),
        ids=DeterministicIdGenerator(),
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(),
        dispatcher=CommandDispatcher(CommandSigner.generate(), clock=_CallableClock()),
    )
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason is not None


@pytest.mark.asyncio
async def test_issue_command_with_low_confidence_becomes_refuse():
    """The agent's confidence (0.3) is below the autonomous threshold
    (default 0.85). The guardrails refuse.
    """
    payload = IssueCommandPayload(
        command_type="capture_photo",
        params={},
        idempotency_key="user-1-photo-low",
        confidence=0.3,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=0.3,
        command=payload,
    )
    llm = FakeAgentLLM(parsed=parsed)
    planner = _build_planner(llm)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason is not None


# --- dispatcher failure (H3-style) -----------------------------------------


@pytest.mark.asyncio
async def test_issue_command_with_dispatcher_failure_becomes_refuse():
    """If the dispatcher raises (e.g. signing error), the Planner
    catches the exception and returns a refusal. The audit log
    records the failure (H3: best-effort).
    """
    payload = IssueCommandPayload(
        command_type="capture_photo",
        params={},
        idempotency_key="user-1-photo-broken",
        confidence=0.9,
    )
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=0.9,
        command=payload,
    )
    llm = FakeAgentLLM(parsed=parsed)

    class _BrokenDispatcher(CommandDispatcher):
        def issue(self, command: Command):
            raise RuntimeError("signing failed")

    planner = _build_planner(llm, dispatcher=_BrokenDispatcher(
        CommandSigner.generate(), clock=_CallableClock()
    ))
    result = await planner.plan(_ctx())
    # H3: a failure in the dispatch path is a refusal, not a crash.
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason is not None
    # The user-facing message mentions the underlying failure so the
    # operator can debug (without leaking internals to the wire).
    assert "issue" in (result.refusal_message or "").lower() or "fail" in (result.refusal_message or "").lower()


# --- non-command paths still work -----------------------------------------


@pytest.mark.asyncio
async def test_answer_path_still_works():
    """The P2-answers path is unchanged. ANSWER is still the default
    when the LLM doesn't emit ISSUE_COMMAND.
    """
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="You said: hello",
        atom_ids=("a1",),
        confidence=0.9,
    )
    llm = FakeAgentLLM(parsed=parsed)
    planner = _build_planner(llm)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.RETURN
    assert result.answer == "You said: hello"
    assert result.command_id is None
    assert result.command_status is None


@pytest.mark.asyncio
async def test_no_memory_path_still_works():
    """The P2-answers no_memory path is unchanged."""
    parsed = AgentAction(
        kind=AgentActionKind.NO_MEMORY,
        text="",
        atom_ids=(),
        confidence=0.9,
    )
    llm = FakeAgentLLM(parsed=parsed)
    planner = _build_planner(llm)
    result = await planner.plan(_ctx())
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason == RejectionReason.NO_SUPPORTING_MEMORY
    assert result.command_id is None


# --- live-wire end-to-end: post-batch-1 expected behavior ------------------
#
# The four server fixes in this slice (#1 strict parser, #2 context
# carve-out, #3 IssueCommandPayload parsing, #4 command-aware
# validator) make the following live `curl /agent` path work
# end-to-end: the planner reaches the LLM, the LLM emits the v2
# issue_command wire shape, the parser keeps the payload, the
# generic validator passes, and the StrictCommandGuardrails refuses
# with `capability_unavailable` because the default
# `ConstantCapabilityProvider` advertises `camera=False`.
#
# This test pins that exact result. A future flip of the
# capability default (or an env-var override) is the ONLY thing
# that should change the live `curl /agent -d '{"text":"record a 3
# second video"}'` behavior to `outcome: "issue_command"` with a
# `command_id`. Until then, the live result is the explicit refusal
# captured here. If this test ever starts asserting
# `outcome == ISSUE_COMMAND`, an operator has explicitly enabled
# camera capture — that is a deliberate configuration change, not
# a silent default flip.


@pytest.mark.asyncio
async def test_record_video_with_default_capabilities_is_refused_with_capability_unavailable():
    """End-to-end: the live `curl /agent -d '{"text":"record a 3
    second video"}'` path, exercised through the real
    OpenAICompatibleAgentLLM (so the v2 wire shape is actually
    parsed, not a pre-constructed AgentAction) and the default
    ConstantCapabilityProvider (so camera=False is the actual
    production default).

    Post-batch-1 expected result:

    1. Planner reaches the LLM (the empty-retrieval carve-out
       no longer short-circuits the LLM call for device actions).
    2. The LLM emits the v2 wire shape; OpenAICompatibleAgentLLM
       parses the `command` field into an IssueCommandPayload.
    3. The generic validator passes (issue_command skips the
       NO_ATOM_CITED check and the payload is non-None).
    4. The strict command validator passes (record_video with
       duration_s=3 is in bounds).
    5. The strict command guardrails REFUSE with
       `capability_unavailable` because ConstantCapabilityProvider
       defaults `camera=False`.

    The live result is an explicit refusal with a user-facing
    message that names the missing capability. A command_id is
    NOT issued; the device is not asked to do anything it cannot
    do. The refusal is observable in the audit log.

    If this test starts failing, EITHER the four batch-1 fixes
    regressed (which would be a code bug), OR the
    ConstantCapabilityProvider default changed (which would be a
    deliberate configuration change). Either way, the test
    forces the change to be explicit.
    """
    # The exact v2 wire shape a chat-tuned model emits for
    # 'record a 3 second video'. The user-facing text is at the
    # top level; the structured payload is in `command`; confidence
    # is at the top level.
    llm_raw_reply = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.92,
        "command": {
            "command_type": "record_video",
            "params": {"duration_s": 3},
            "idempotency_key": "record_video_3s",
        },
    })

    class _ScriptedChat:
        """A synchronous chat model that returns a fixed reply."""

        def complete(self, system: str, user: str) -> str:
            return llm_raw_reply

    # Empty retrieval: post-#2 the LLM is not biased against
    # commands, and the planner is not short-circuited.
    class _EmptyRetriever:
        def retrieve(self, *args, **kwargs):
            return RetrievedContext(
                atoms=(),
                retrieval_strategy="sim_recency",
                scorer_version="v1",
                index_name="in_memory",
                index_version="v1",
                top_score=0.0,
                lowest_score=0.0,
                returned_count=0,
                candidate_count=0,
                retrieval_latency_ms=10,
                retrieval_trace_id="trace-empty",
            )

    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_CallableClock())
    planner = Planner(
        retriever=_EmptyRetriever(),
        context_builder=ContextBuilder(),
        llm=OpenAICompatibleAgentLLM(_ScriptedChat()),
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=InMemoryMetricsRecorder(),
        # The default ConstantCapabilityProvider — camera=False
        # is the production default. If this changes, the
        # assertion below changes too.
        capability_provider=ConstantCapabilityProvider(),
        clock=_CallableClock(),
        ids=DeterministicIdGenerator(),
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(),
        dispatcher=dispatcher,
    )
    result = await planner.plan(_ctx())

    # The planner reached the LLM, parsed the payload, validated
    # the action, and the guardrails refused with the right
    # reason. No command was issued.
    assert result.outcome == PlannerOutcome.REFUSE
    assert result.command_id is None
    assert result.command_status is None
    # The refusal reason is the strict-command-guardrails
    # CAPABILITY_UNAVAILABLE (not the generic validator's
    # NO_ATOM_CITED, which would mean the four batch-1 fixes
    # regressed). The generic RejectionReason set does not have
    # a code for the command-guardrails' distinct enum, so the
    # Planner maps it. We assert the user-facing message names
    # the missing capability so the operator / user can act.
    assert result.refusal_reason is not None
    assert result.refusal_message is not None
    msg = result.refusal_message.lower()
    assert "camera" in msg, (
        f"refusal message should name the missing capability "
        f"('camera'), got: {result.refusal_message!r}"
    )
    # And the dispatcher did not issue anything.
    assert dispatcher.pending() == []
