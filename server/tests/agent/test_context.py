"""Tests for ContextBuilder (H1)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openrecall_server.agent.context import ContextBuilder
from openrecall_server.contracts.types import (
    CapabilitySet,
    Prompt,
    RetrievedContext,
    ScoredAtom,
    UserRequest,
)


def _atom(atom_id: str, text: str, session_id: str = "s1") -> ScoredAtom:
    return ScoredAtom(
        atom_id=atom_id,
        session_id=session_id,
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=0.9,
    )


def test_context_builder_marks_prompt_versions():
    rc = RetrievedContext(atoms=(_atom("a1", "x"),))
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="hello"), rc, capabilities=CapabilitySet())
    assert prompt.system_prompt_version == "v2"
    assert prompt.context_builder_version == "v1"


def test_context_builder_includes_retrieved_atoms_with_delimiters():
    rc = RetrievedContext(atoms=(
        _atom("a1", "alpha"),
        _atom("a2", "beta"),
    ))
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="what?"), rc, capabilities=CapabilitySet())
    # The system prompt carries the atom block with [id] delimiters.
    assert "[a1]" in prompt.system
    assert "[a2]" in prompt.system
    assert "alpha" in prompt.system
    assert "beta" in prompt.system
    # The user prompt carries the available ids as a flat list.
    assert "a1" in prompt.user
    assert "a2" in prompt.user


def test_context_builder_includes_capabilities_in_system():
    rc = RetrievedContext(atoms=())
    cb = ContextBuilder()
    caps = CapabilitySet(camera=True, microphone=False)
    prompt = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=caps)
    assert "camera" in prompt.system
    assert "microphone" in prompt.system


def test_context_builder_no_supporting_memory_short_circuits():
    """If retrieval returned no atoms AND the user is asking a factual
    question, the system prompt carries the 'no supporting memories'
    line so the LLM is biased toward ``no_memory`` rather than a
    hallucinated answer. The carve-out for direct commands is in a
    separate test.
    """
    rc = RetrievedContext(atoms=())
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="what time did I go to the gym?"), rc, capabilities=CapabilitySet())
    assert "no supporting" in prompt.system.lower() or "no relevant" in prompt.system.lower()


def test_context_builder_empty_retrieval_preserves_command_carve_out():
    """Empty retrieval must NOT bias the LLM away from issue_command.

    Previously, ContextBuilder appended 'No relevant memory was
    retrieved. You must return kind="no_memory".' on every empty
    retrieval, which short-circuited direct commands like
    'record a 3 second video' even when the rest of the prompt
    documented issue_command as the correct path. The new contract:

    - Factual questions with no retrieval → bias toward no_memory.
    - Device-action requests with no retrieval → bias toward
      issue_command (an empty retrieval is normal and expected
      for a /agent request that the user just typed).

    The command carve-out is in the system prompt itself, not just
    in the appended line, so it survives any future prompt refactor.
    """
    rc = RetrievedContext(atoms=())
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="record a 3 second video"), rc, capabilities=CapabilitySet())
    # The command option is still documented (it was documented in v2
    # already, but the no-memory line used to contradict it for empty
    # retrieval). The carve-out is explicit.
    assert "issue_command" in prompt.system
    # The no-memory hint is now conditional on a factual question,
    # not an unconditional instruction. The old line was:
    #     "No relevant memory was retrieved. You must return kind=\"no_memory\"."
    # The new wording explicitly carves out device actions.
    lower = prompt.system.lower()
    assert "device action" in lower, (
        "Empty-retrieval directive does not mention device actions; "
        "direct commands will be short-circuited to no_memory."
    )
    # And the no-memory bias is explicitly scoped to factual questions
    # so the LLM is not biased against commands.
    assert "factual" in lower, (
        "No-memory hint is no longer conditional on a factual question."
    )


def test_context_builder_empty_retrieval_command_carve_out_does_not_weaken_factual_questions():
    """Symmetric guarantee: the carve-out for commands must NOT
    weaken the no-memory bias for factual questions. The LLM should
    not start hallucinating answers to 'what time did I eat lunch?'
    just because we made commands orthogonal to retrieval.
    """
    rc = RetrievedContext(atoms=())
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="what time did I eat lunch yesterday?"), rc, capabilities=CapabilitySet())
    # The no-memory bias is still there for factual questions.
    assert "no supporting" in prompt.system.lower() or "no relevant" in prompt.system.lower()
    # And the LLM is still told not to guess.
    assert "never guess" in prompt.system.lower() or "do not invent" in prompt.system.lower() or "do not guess" in prompt.system.lower()


def test_context_builder_prompt_is_pure_for_same_inputs():
    rc = RetrievedContext(atoms=(_atom("a1", "x"),))
    cb = ContextBuilder()
    p1 = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    p2 = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    # Pydantic frozen model + identical inputs => identical objects.
    assert p1.system == p2.system
    assert p1.user == p2.user


def test_context_builder_v2_prompt_documents_command_option():
    """The v2 system prompt must mention issue_command AND every one
    of the 5 valid command types so the LLM knows the option exists
    and produces payloads the CommandValidator will accept."""
    rc = RetrievedContext(atoms=(
        _atom("a1", "x"),
    ))
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    assert prompt.system_prompt_version == "v2"
    # The command option must be documented.
    assert "issue_command" in prompt.system
    # All 5 P2 command types must be in the prompt.
    for cmd_type in ("capture_photo", "record_video", "start_audio", "stop_audio", "request_buffer"):
        assert cmd_type in prompt.system, f"command type {cmd_type!r} missing from v2 prompt"
    # The LLM must know to include an idempotency_key + confidence.
    assert "idempotency_key" in prompt.system
    assert "confidence" in prompt.system
