"""Strict-JSON Validator for LLM agent output (M2.4 / M10).

The Validator is a pure function over the LLM result + a small context.
It does no I/O, holds no state, and is safe to call from any thread. The
binding rules are:

  1. ``parse_error is not None``  -> ``INVALID_JSON``
  2. ``NO_MEMORY`` with any atom_ids  -> ``SCHEMA_MISMATCH``
  3. ``ISSUE_COMMAND`` without a parsed command payload
     -> ``MISSING_COMMAND_PAYLOAD``
  4. ``ANSWER`` with empty atom_ids  -> ``NO_ATOM_CITED``
  5. ``ANSWER`` with any cited id not in the retrieved set
     -> ``CITED_ATOM_NOT_RETRIEVED``
  6. ``ANSWER`` with confidence outside [0, 1]
     -> ``CONFIDENCE_OUT_OF_RANGE``
  7. otherwise  -> accepted

The ``ISSUE_COMMAND`` branch is intentionally minimal: the generic
validator only enforces the wire-shape contract (the kind is set
and a parsed :class:`IssueCommandPayload` is present). The 5-type
allowlist and per-type param bounds are owned by
:class:`openrecall_server.agent.validator_command.StrictCommandValidator`,
which is the sole authority for command-type / param-bounds policy.
Splitting the two means a change to bounds does not require a
generic-validator regression test, and a change to the wire shape
does not require a command-bounds regression test.
"""
from __future__ import annotations

from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    LLMResult,
    RejectionReason,
    ValidatedAction,
    ValidatorContext,
)


def _stub_action() -> AgentAction:
    """Placeholder used when the LLM result couldn't be parsed at all.

    The action is never surfaced to the user; it exists only so the
    :class:`ValidatedAction` shape is preserved end-to-end.
    """
    return AgentAction(
        kind=AgentActionKind.NO_MEMORY,
        text="",
        atom_ids=(),
        confidence=0.0,
    )


class StrictJSONValidator:
    """Enforces the agent-action contract over a parsed LLM result.

    Stateless and dependency-free. Test fakes are not needed — instantiate
    and call.
    """

    def validate(
        self, ctx: ValidatorContext, result: LLMResult
    ) -> ValidatedAction:
        # 1. Parse error short-circuits.
        if result.parsed is None:
            return ValidatedAction(
                action=_stub_action(),
                rejection=RejectionReason.INVALID_JSON,
            )

        action = result.parsed
        retrieved = set(ctx.retrieved_atom_ids)

        # 2. NO_MEMORY must carry no atom_ids.
        if action.kind == AgentActionKind.NO_MEMORY:
            if action.atom_ids:
                return ValidatedAction(
                    action=action,
                    rejection=RejectionReason.SCHEMA_MISMATCH,
                )
            return ValidatedAction(action=action, rejection=None)

        # 3. P2-commands: ISSUE_COMMAND skips the answer-style
        # citation checks (an empty atom_ids is the v2 prompt's
        # contracted shape for a device action) and instead requires
        # a parsed IssueCommandPayload. The 5-type allowlist and
        # per-type param bounds are owned by StrictCommandValidator
        # — the generic validator does NOT duplicate those rules.
        if action.kind == AgentActionKind.ISSUE_COMMAND:
            if action.command is None:
                return ValidatedAction(
                    action=action,
                    rejection=RejectionReason.MISSING_COMMAND_PAYLOAD,
                )
            return ValidatedAction(action=action, rejection=None)

        # 4. ANSWER must cite at least one retrieved atom.
        if not action.atom_ids:
            return ValidatedAction(
                action=action,
                rejection=RejectionReason.NO_ATOM_CITED,
            )

        # 5. All cited atoms must come from the retrieved set.
        cited = set(action.atom_ids)
        if not cited.issubset(retrieved):
            return ValidatedAction(
                action=action,
                rejection=RejectionReason.CITED_ATOM_NOT_RETRIEVED,
            )

        # 6. Confidence is bounded.
        if not (0.0 <= action.confidence <= 1.0):
            return ValidatedAction(
                action=action,
                rejection=RejectionReason.CONFIDENCE_OUT_OF_RANGE,
            )

        # 7. Accepted.
        return ValidatedAction(action=action, rejection=None)
