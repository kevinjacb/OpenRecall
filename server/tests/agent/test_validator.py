"""Tests for the strict-JSON Validator (M10).

The Validator enforces four things in order:
  1. the raw output is valid JSON and parses to the action schema;
  2. cited atom_ids were actually retrieved (no hallucinations);
  3. confidence is in [0, 1];
  4. NO_ATOM_CITED when the model answers without citing any retrieved atom.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    Confidence,
    LLMResult,
    RejectionReason,
    ValidatorContext,
)
from sense_server.agent.validator import StrictJSONValidator


def _ctx(atoms=("a1", "a2"), threshold: float = 0.3) -> ValidatorContext:
    return ValidatorContext(
        retrieved_atom_ids=atoms,
        no_memory_top_score_threshold=threshold,
    )


def test_validator_accepts_answer_with_cited_atom():
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="You mentioned X yesterday.",
        atom_ids=("a1",),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw='{"kind":"answer",...}', parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None
    assert out.action is parsed


def test_validator_rejects_answer_without_atoms():
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=(),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.NO_ATOM_CITED


def test_validator_rejects_answer_with_unretrieved_atom():
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=("a_evil",),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.CITED_ATOM_NOT_RETRIEVED


def test_validator_rejects_parse_error():
    v = StrictJSONValidator()
    res = LLMResult(raw="not json", parsed=None, parse_error="expecting ',' delimiter")
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.INVALID_JSON


def test_validator_accepts_no_memory_with_empty_atom_ids():
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.NO_MEMORY,
        text="No relevant memory found.",
        atom_ids=(),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None


def test_validator_rejects_no_memory_with_atom_ids():
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.NO_MEMORY,
        text="x",
        atom_ids=("a1",),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.SCHEMA_MISMATCH


def test_validator_rejects_answer_with_partial_unretrieved_atoms():
    """If the LLM cites one valid + one fabricated atom, reject."""
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=("a1", "a_fabricated"),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.CITED_ATOM_NOT_RETRIEVED


def test_agent_action_confidence_rejects_out_of_range():
    with pytest.raises(ValidationError):
        AgentAction(
            kind=AgentActionKind.ANSWER,
            text="x",
            atom_ids=("a1",),
            confidence=Confidence(1.5),  # type: ignore[arg-type]
        )
