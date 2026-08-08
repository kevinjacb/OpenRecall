"""Tests for the pure PlannerResult → DTO mapper (N1.2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from opensapien_server.contracts.types import (
    PlannerOutcome,
    PlannerResult,
    RejectionReason,
    ScoredAtom,
)
from opensapien_server.http.routes.dto import (
    AgentResponseDTO,
    MAX_ATOM_CHIP_TEXT_LEN,
)
from opensapien_server.http.routes.mapper import map_planner_result_to_dto


def _atom(atom_id: str, text: str = "x", score: float = 0.5) -> ScoredAtom:
    return ScoredAtom(
        atom_id=atom_id,
        session_id="s1",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=score,
    )


def _result(**kwargs) -> PlannerResult:
    base = dict(
        request_id="req-1",
        retrieval_trace_id="trace-1",
        audit_id="audit-1",
        outcome=PlannerOutcome.RETURN,
        answer="x",
        confidence=0.9,
        confidence_band="high",
        atom_ids=("a1",),
    )
    base.update(kwargs)
    return PlannerResult(**base)


def test_mapper_return_outcome_carries_answer_and_atoms():
    atoms = (_atom("a1", "hello"),)
    result = _result(atoms=atoms)
    dto = map_planner_result_to_dto(result)
    assert dto.outcome == "return"
    assert dto.answer == "x"
    assert dto.confidence == 0.9
    assert dto.confidence_band == "high"
    assert len(dto.atoms) == 1
    assert dto.atoms[0].atom_id == "a1"
    assert dto.atoms[0].text == "hello"
    assert dto.request_id == "req-1"
    assert dto.retrieval_trace_id == "trace-1"
    assert dto.audit_id == "audit-1"


def test_mapper_refuse_outcome_carries_refusal_message():
    result = _result(
        outcome=PlannerOutcome.REFUSE,
        answer=None,
        confidence=None,
        atom_ids=(),
        refusal_message="No relevant memory found.",
    )
    dto = map_planner_result_to_dto(result)
    assert dto.outcome == "refuse"
    assert dto.answer is None
    assert dto.refusal_reason == "No relevant memory found."
    assert dto.atoms == []


def test_mapper_return_with_uncertainty_outcome():
    result = _result(
        outcome=PlannerOutcome.RETURN_WITH_UNCERTAINTY,
        confidence_band="medium",
    )
    dto = map_planner_result_to_dto(result)
    assert dto.outcome == "return_with_uncertainty"


def test_mapper_truncates_long_text_to_chip_limit():
    long = "x" * 1000
    atoms = (_atom("a1", long),)
    result = _result(atoms=atoms)
    dto = map_planner_result_to_dto(result)
    # chip text is truncated to MAX_ATOM_CHIP_TEXT_LEN + 1 (the ellipsis)
    assert len(dto.atoms[0].text) == MAX_ATOM_CHIP_TEXT_LEN
    assert dto.atoms[0].text.endswith("…")


def test_mapper_short_text_not_truncated():
    short = "x" * 50
    atoms = (_atom("a1", short),)
    result = _result(atoms=atoms)
    dto = map_planner_result_to_dto(result)
    assert dto.atoms[0].text == short


def test_mapper_skips_atom_ids_not_in_result_atoms():
    """If the result cites an id with no corresponding atom, the chip
    is skipped (defensive)."""
    atoms = (_atom("a1"),)
    result = _result(atoms=atoms, atom_ids=("a1", "a_missing"))
    dto = map_planner_result_to_dto(result)
    assert [c.atom_id for c in dto.atoms] == ["a1"]


def test_mapper_handles_no_audit_id():
    result = _result(audit_id=None)
    dto = map_planner_result_to_dto(result)
    assert dto.audit_id == ""


def test_mapper_preserves_score_on_chips():
    atoms = (_atom("a1", score=0.42),)
    result = _result(atoms=atoms)
    dto = map_planner_result_to_dto(result)
    assert dto.atoms[0].score == 0.42
