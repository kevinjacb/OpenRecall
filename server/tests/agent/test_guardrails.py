"""Tests for the Guardrails."""
from __future__ import annotations

import pytest

from opensapien_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    GuardOutcome,
    RejectionReason,
    ValidatedAction,
    ValidatorContext,
)
from opensapien_server.agent.guardrails import ConfidenceGateGuardrails


def _validated(kind=AgentActionKind.ANSWER, confidence=0.9, atom_ids=("a1",), rejection=None) -> ValidatedAction:
    return ValidatedAction(
        action=AgentAction(
            kind=kind,
            text="x",
            atom_ids=atom_ids,
            confidence=confidence,
        ),
        rejection=rejection,
    )


def test_guardrails_accepts_high_confidence_answer():
    g = ConfidenceGateGuardrails()
    out = g.decide(_validated(confidence=0.95), ValidatorContext(retrieved_atom_ids=("a1",)))
    assert out.outcome == GuardOutcome.RETURN
    assert out.refusal_reason is None


def test_guardrails_returns_with_uncertainty_for_medium_confidence():
    g = ConfidenceGateGuardrails()
    out = g.decide(_validated(confidence=0.7), ValidatorContext(retrieved_atom_ids=("a1",)))
    assert out.outcome == GuardOutcome.RETURN_WITH_UNCERTAINTY


def test_guardrails_refuses_low_confidence_answer():
    g = ConfidenceGateGuardrails()
    out = g.decide(_validated(confidence=0.4), ValidatorContext(retrieved_atom_ids=("a1",)))
    assert out.outcome == GuardOutcome.REFUSE
    assert out.refusal_reason == RejectionReason.NOT_AUTONOMOUS


def test_guardrails_refuses_no_memory():
    g = ConfidenceGateGuardrails()
    out = g.decide(
        _validated(kind=AgentActionKind.NO_MEMORY, atom_ids=()),
        ValidatorContext(retrieved_atom_ids=()),
    )
    assert out.outcome == GuardOutcome.REFUSE
    assert out.refusal_reason == RejectionReason.NO_SUPPORTING_MEMORY
    assert out.refusal_message is not None


def test_guardrails_refuses_when_validator_rejected():
    g = ConfidenceGateGuardrails()
    out = g.decide(
        _validated(rejection=RejectionReason.NO_ATOM_CITED),
        ValidatorContext(retrieved_atom_ids=()),
    )
    assert out.outcome == GuardOutcome.REFUSE
    assert out.refusal_reason == RejectionReason.NO_ATOM_CITED


def test_guardrails_rate_limits():
    g = ConfidenceGateGuardrails(rate_limit_per_min=2)
    ctx = ValidatorContext(retrieved_atom_ids=("a1",))
    assert g.decide(_validated(confidence=0.9), ctx).outcome == GuardOutcome.RETURN
    assert g.decide(_validated(confidence=0.9), ctx).outcome == GuardOutcome.RETURN
    out = g.decide(_validated(confidence=0.9), ctx)
    assert out.outcome == GuardOutcome.REFUSE
    assert out.refusal_reason == RejectionReason.RATE_LIMIT


def test_guardrails_thresholds_configurable():
    g = ConfidenceGateGuardrails(
        confidence_autonomous=0.99, confidence_confirm=0.5, rate_limit_per_min=1000
    )
    # 0.7 with new thresholds: 0.7 < 0.99 (autonomous) but 0.7 >= 0.5 (confirm)
    # → RETURN_WITH_UNCERTAINTY
    out = g.decide(_validated(confidence=0.7), ValidatorContext(retrieved_atom_ids=("a1",)))
    assert out.outcome == GuardOutcome.RETURN_WITH_UNCERTAINTY
    # 0.4 is below the new confirm threshold — REFUSE.
    out = g.decide(_validated(confidence=0.4), ValidatorContext(retrieved_atom_ids=("a1",)))
    assert out.outcome == GuardOutcome.REFUSE
