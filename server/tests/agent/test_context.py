"""Tests for ContextBuilder (H1)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sense_server.agent.context import ContextBuilder
from sense_server.contracts.types import (
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
    assert prompt.system_prompt_version == "v1"
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
    """If retrieval returned no atoms, the system prompt carries the
    'no supporting memories' line so the LLM is biased toward
    ``no_memory`` rather than a hallucinated answer."""
    rc = RetrievedContext(atoms=())
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    assert "no supporting" in prompt.system.lower() or "no relevant" in prompt.system.lower()


def test_context_builder_prompt_is_pure_for_same_inputs():
    rc = RetrievedContext(atoms=(_atom("a1", "x"),))
    cb = ContextBuilder()
    p1 = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    p2 = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    # Pydantic frozen model + identical inputs => identical objects.
    assert p1.system == p2.system
    assert p1.user == p2.user
