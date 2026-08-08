"""Tests for the transport DTOs (N1.1).

The DTOs are the wire shape for the agent + memory endpoints. They
must be frozen, strict (``extra="forbid"``), and carry a stable
``schema_version``. Any future field added at the server side will
be rejected here until the client is updated — the test enforces
that explicitly.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from openrecall_server.http.routes.dto import (
    SCHEMA_VERSION,
    AgentRequestDTO,
    AgentResponseDTO,
    AtomChipDTO,
    ErrorEnvelopeDTO,
    MemoryAtomDTO,
    MemorySearchResponseDTO,
    SessionMemoryResponseDTO,
)


def test_dto_schema_version_constant_is_v1():
    assert SCHEMA_VERSION == "v1"


def test_agent_request_dto_minimal_valid():
    r = AgentRequestDTO(text="hello")
    assert r.schema_version == "v1"
    assert r.text == "hello"
    assert r.session_id is None
    assert r.limit == 10


def test_agent_request_dto_rejects_empty_text():
    with pytest.raises(ValidationError):
        AgentRequestDTO(text="")


def test_agent_request_dto_rejects_extra_fields():
    with pytest.raises(ValidationError, match="extra"):
        AgentRequestDTO(text="x", unknown_field="boom")


def test_agent_request_dto_caps_limit():
    with pytest.raises(ValidationError):
        AgentRequestDTO(text="x", limit=1000)


def test_agent_response_dto_return_outcome():
    r = AgentResponseDTO(
        request_id="r1",
        retrieval_trace_id="t1",
        audit_id="a1",
        outcome="return",
        answer="x",
        confidence=0.9,
        confidence_band="high",
    )
    assert r.schema_version == "v1"
    assert r.outcome == "return"
    assert r.answer == "x"
    assert r.refusal_reason is None


def test_agent_response_dto_refuse_outcome():
    r = AgentResponseDTO(
        request_id="r1",
        retrieval_trace_id="t1",
        audit_id="a1",
        outcome="refuse",
        refusal_reason="no_supporting_memory",
    )
    assert r.outcome == "refuse"
    assert r.answer is None
    assert r.refusal_reason == "no_supporting_memory"


def test_agent_response_dto_rejects_unknown_outcome():
    with pytest.raises(ValidationError):
        AgentResponseDTO(
            request_id="r1",
            retrieval_trace_id="t1",
            audit_id="a1",
            outcome="explode",  # type: ignore[arg-type]
        )


def test_atom_chip_dto_is_frozen():
    chip = AtomChipDTO(
        atom_id="a1",
        session_id="s1",
        kind="fact",
        text="x",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=0.5,
    )
    with pytest.raises(ValidationError):
        chip.text = "mutated"  # type: ignore[misc]


def test_memory_atom_dto_round_trip():
    m = MemoryAtomDTO(
        atom_id="a1",
        session_id="s1",
        kind="fact",
        text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        source_event_id="e1",
        source_modality="transcript",
        extraction_version="v1",
        embedding_model="bge-small",
        extractor_prompt_version="v1",
    )
    assert m.schema_version == "v1"
    assert m.embedding_model == "bge-small"


def test_memory_search_response_dto_minimal():
    r = MemorySearchResponseDTO(
        request_id="r1",
        retrieval_trace_id="t1",
        audit_id="a1",
        query="x",
    )
    assert r.atoms == []
    assert r.returned_count == 0


def test_session_memory_response_dto_minimal():
    r = SessionMemoryResponseDTO(session_id="s1")
    assert r.atoms == []
    assert r.returned_count == 0


def test_error_envelope_dto():
    e = ErrorEnvelopeDTO(code="bad_request", message="x", request_id="r1")
    assert e.code == "bad_request"
    assert e.request_id == "r1"


def test_error_envelope_dto_rejects_unknown_code():
    with pytest.raises(ValidationError):
        ErrorEnvelopeDTO(code="unknown_code", message="x")  # type: ignore[arg-type]
