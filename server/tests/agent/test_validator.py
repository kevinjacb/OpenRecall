"""Tests for the strict-JSON Validator (M10).

The Validator enforces the agent-action contract over a parsed LLM
result. Rules, in order:

  1. ``parse_error is not None``  -> ``INVALID_JSON``
  2. ``NO_MEMORY`` with any atom_ids  -> ``SCHEMA_MISMATCH``
  3. ``ANSWER`` with empty atom_ids  -> ``NO_ATOM_CITED``
  4. ``ANSWER`` with any cited id not in the retrieved set
     -> ``CITED_ATOM_NOT_RETRIEVED``
  5. ``ANSWER`` with confidence outside [0, 1]
     -> ``CONFIDENCE_OUT_OF_RANGE``

For ``ISSUE_COMMAND`` (P2-commands): the validator skips the
``NO_ATOM_CITED`` and ``CITED_ATOM_NOT_RETRIEVED`` checks (an empty
``atom_ids`` is the v2 prompt's contracted shape) and instead
requires a parsed ``IssueCommandPayload`` on the action. The 5-type
allowlist and per-type param bounds are owned by
:class:`StrictCommandValidator` (a separate module); the generic
validator does not duplicate that logic.
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from openrecall_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    Confidence,
    IssueCommandPayload,
    LLMResult,
    RejectionReason,
    ValidatorContext,
)
from openrecall_server.agent.validator import StrictJSONValidator


def _ctx(atoms=("a1", "a2"), threshold: float = 0.3) -> ValidatorContext:
    return ValidatorContext(
        retrieved_atom_ids=atoms,
        no_memory_top_score_threshold=threshold,
    )


def _command_payload(
    command_type: str = "capture_photo",
    params: dict | None = None,
    idempotency_key: str = "ik-1",
    confidence: float = 0.9,
) -> IssueCommandPayload:
    return IssueCommandPayload(
        command_type=command_type,
        params=params or {},
        idempotency_key=idempotency_key,
        confidence=confidence,
    )


def _raw_command(
    command_type: str,
    params: dict | None = None,
    idempotency_key: str = "ik-1",
    confidence: float = 0.9,
) -> IssueCommandPayload:
    """Bypass the IssueCommandPayload Literal allowlist for negative
    tests that need a payload with an "invalid" command_type.

    The generic validator must not duplicate the 5-type allowlist
    check (that is owned by StrictCommandValidator), so the negative
    test needs to present a payload whose command_type is outside
    the allowlist. pydantic's Literal enforcement would refuse to
    construct such a payload via the normal constructor; using
    ``model_construct`` is the documented escape hatch for tests.
    """
    return IssueCommandPayload.model_construct(
        command_type=command_type,
        params=params or {},
        idempotency_key=idempotency_key,
        confidence=confidence,
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


# --- issue_command (P2-commands) -------------------------------------------


def test_validator_accepts_issue_command_with_empty_atom_ids_and_payload():
    """The v2 system prompt contracts issue_command to carry empty
    atom_ids (the structured payload replaces the free-form citation
    list). The validator must NOT reject this with NO_ATOM_CITED —
    that was the previous contract, and it meant every issue_command
    reply was refused at the validator before the planner could see
    it.
    """
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=Confidence(0.9),
        command=_command_payload(command_type="record_video", params={"duration_s": 3}),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None
    assert out.action is parsed


def test_validator_rejects_issue_command_without_command_payload():
    """An issue_command reply with no parsed IssueCommandPayload is a
    contract violation — the parser already enforces this (kind +
    payload are required together), but a future parser regression
    or a constructed AgentAction (e.g. in a test) must not slip
    through.
    """
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=Confidence(0.9),
        command=None,
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.MISSING_COMMAND_PAYLOAD


def test_validator_does_not_check_atom_citation_for_issue_command():
    """issue_command replies skip the retrieved-set check entirely.
    The Citation discipline is a property of the answer path; the
    command path is a device-action contract, not a memory citation.
    """
    v = StrictJSONValidator()
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=("nonexistent_atom",),  # would be a fabricated id for an answer
        confidence=Confidence(0.9),
        command=_command_payload(),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(atoms=("a1", "a2")), res)
    assert out.rejection is None


def test_validator_does_not_duplicate_command_type_or_param_bounds_checks():
    """The 5-type allowlist and per-type param bounds are owned by
    StrictCommandValidator (a separate module). The generic validator
    must NOT reject an issue_command for an unknown command_type or
    an out-of-range param — that would be a duplicate policy surface
    and would mean the planner's call site has to bypass one of the
    two to test the other. The generic validator only enforces the
    wire-shape contract (kind + payload present + parsed)."""
    v = StrictJSONValidator()
    # A command_type outside the 5-type allowlist. The generic
    # validator must accept it (the wire shape is valid; the
    # downstream StrictCommandValidator will reject with
    # UNKNOWN_TYPE).
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=Confidence(0.9),
        command=_raw_command(command_type="launch_missiles"),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None

    # A param out of range for record_video (duration_s > 30). The
    # generic validator must accept it; StrictCommandValidator owns
    # the bounds.
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=(),
        confidence=Confidence(0.9),
        command=_command_payload(
            command_type="record_video", params={"duration_s": 999}
        ),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None


def test_validator_preserves_no_memory_and_answer_behavior_unchanged():
    """The new issue_command branch must NOT regress the existing
    answer / no_memory rules. This is a structural-regression
    guard: a future refactor that re-orders the branches must keep
    the existing semantics.
    """
    v = StrictJSONValidator()

    # NO_MEMORY with empty atom_ids still passes.
    parsed = AgentAction(
        kind=AgentActionKind.NO_MEMORY,
        text="No relevant memory found.",
        atom_ids=(),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None

    # ANSWER with empty atom_ids still rejected with NO_ATOM_CITED.
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=(),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.NO_ATOM_CITED

    # ANSWER with a fabricated atom still rejected.
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=("fabricated",),
        confidence=Confidence(0.9),
    )
    res = LLMResult(raw="...", parsed=parsed, parse_error=None)
    out = v.validate(_ctx(atoms=("a1",)), res)
    assert out.rejection == RejectionReason.CITED_ATOM_NOT_RETRIEVED
