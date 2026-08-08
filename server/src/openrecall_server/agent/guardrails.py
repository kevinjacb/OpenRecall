"""Guardrails — confidence-gated autonomy for the cognitive read path.

The P2-answers slice uses three guardrails:

  1. **Confidence gate** — at or above ``confidence_autonomous``, the
     action is :class:`GuardOutcome.RETURN`; below
     ``confidence_confirm`, it is :class:`GuardOutcome.RETURN_WITH_UNCERTAINTY`;
     below the floor, it is :class:`GuardOutcome.REFUSE`.
  2. **Rate limit** — N actions per minute per session, dropping
     excess as :class:`GuardOutcome.REFUSE` with ``RATE_LIMIT``.
  3. **Short-circuit on no memory** — if the Validator signals
     ``NO_SUPPORTING_MEMORY`` (or the LLM result is ``no_memory``),
     return :class:`GuardOutcome.REFUSE` with a friendly message.
"""
from __future__ import annotations

import time
from collections import deque
from typing import Protocol, runtime_checkable

from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    Confidence,
    GuardOutcome,
    GuardedAction,
    RejectionReason,
    ValidatedAction,
    ValidatorContext,
)


@runtime_checkable
class Guardrails(Protocol):
    """Single seam for "should we let this action go through"."""

    def decide(
        self,
        validated: ValidatedAction,
        ctx: ValidatorContext,
    ) -> GuardedAction:
        ...


class ConfidenceGateGuardrails:
    """Default guardrails — confidence-gated autonomy + rate limit.

    Stateless except for the rate-limit window. The thresholds are
    configurable; the defaults match the spec.
    """

    def __init__(
        self,
        confidence_autonomous: float = 0.85,
        confidence_confirm: float = 0.60,
        rate_limit_per_min: int = 20,
        clock=None,
    ) -> None:
        self._autonomous = confidence_autonomous
        self._confirm = confidence_confirm
        self._rate_limit = rate_limit_per_min
        self._clock = clock or time.monotonic
        self._rate_window: dict[str, deque[float]] = {}

    def decide(
        self,
        validated: ValidatedAction,
        ctx: ValidatorContext,
    ) -> GuardedAction:
        if validated.rejection is not None:
            return self._on_rejection(validated)
        if validated.action.kind == AgentActionKind.NO_MEMORY:
            return GuardedAction(
                outcome=GuardOutcome.REFUSE,
                action=validated.action,
                refusal_reason=RejectionReason.NO_SUPPORTING_MEMORY,
                refusal_message="No relevant memory found.",
            )
        if not self._rate_ok(ctx):
            return GuardedAction(
                outcome=GuardOutcome.REFUSE,
                action=validated.action,
                refusal_reason=RejectionReason.RATE_LIMIT,
                refusal_message="Too many requests. Please slow down.",
            )
        confidence = validated.action.confidence
        if confidence >= self._autonomous:
            return GuardedAction(
                outcome=GuardOutcome.RETURN,
                action=validated.action,
                refusal_reason=None,
                refusal_message=None,
            )
        if confidence >= self._confirm:
            return GuardedAction(
                outcome=GuardOutcome.RETURN_WITH_UNCERTAINTY,
                action=validated.action,
                refusal_reason=None,
                refusal_message=None,
            )
        return GuardedAction(
            outcome=GuardOutcome.REFUSE,
            action=validated.action,
            refusal_reason=RejectionReason.NOT_AUTONOMOUS,
            refusal_message="Confidence too low to answer.",
        )

    # --- helpers ----------------------------------------------------------

    def _on_rejection(self, validated: ValidatedAction) -> GuardedAction:
        if validated.rejection == RejectionReason.NO_ATOM_CITED:
            return GuardedAction(
                outcome=GuardOutcome.REFUSE,
                action=validated.action,
                refusal_reason=validated.rejection,
                refusal_message="Could you rephrase? I couldn't find a memory to cite.",
            )
        if validated.rejection == RejectionReason.CITED_ATOM_NOT_RETRIEVED:
            return GuardedAction(
                outcome=GuardOutcome.REFUSE,
                action=validated.action,
                refusal_reason=validated.rejection,
                refusal_message="I had trouble citing memory. Please try again.",
            )
        return GuardedAction(
            outcome=GuardOutcome.REFUSE,
            action=validated.action,
            refusal_reason=validated.rejection or RejectionReason.UNKNOWN,
            refusal_message="I couldn't answer that.",
        )

    def _rate_ok(self, ctx: ValidatorContext) -> bool:
        # The ValidatorContext doesn't currently carry a session_id;
        # the Planner passes the session via the enclosing scope. For
        # this slice the rate limit is process-wide.
        sid = "global"
        now = self._clock()
        window = self._rate_window.setdefault(sid, deque())
        # Drop entries older than 60s.
        while window and now - window[0] > 60.0:
            window.popleft()
        if len(window) >= self._rate_limit:
            return False
        window.append(now)
        return True
