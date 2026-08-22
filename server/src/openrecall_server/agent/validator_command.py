"""Command validator: allowlist + per-type param bounds (M10 / §6.2).

The validator runs after the LLM has produced a parsed
:class:`AgentAction` carrying an :class:`IssueCommandPayload`. It
sanitizes the params against the per-type schema and returns either:

  - :class:`ValidatedCommand` (a command with cleaned params ready
    to dispatch), or
  - a :class:`CommandValidationResult` with a non-None ``rejection``
    (the user sees a friendly refusal message; the audit log
    records the reason).

Per-type schemas (per the spec §6.2):

  - ``capture_photo``:   no params
  - ``record_video``:    ``duration_s in [1, 30]``
  - ``start_audio``:     no params
  - ``stop_audio``:      no params
  - ``request_buffer``:  ``seconds in [1, 60]``
  - ``record_audio``:    ``duration_s in [1, 120]``  (P1)
  - ``start_video``:     no params                   (P1)
  - ``stop_video``:      no params                   (P1)
  - ``flush_snapshots``: no params                   (P1)
  - ``sleep``:           no params                   (P2)
  - ``set_snapshot_interval``: ``seconds in [0, 600]`` (P3; 0=off)

The validator also enforces the command allowlist: the 5 P2 types
plus the 4 P1 instruction-processor types plus the P2 ``sleep`` type
plus the P3 ``set_snapshot_interval`` type are autonomously
executable. ``display_text``, ``play_audio``, and ``show_status``
are not allowlisted; they fail validation as ``UNKNOWN_TYPE``.
Adding them is a one-line change in :data:`ALLOWLIST` once a future
phase implements the corresponding firmware executors.

The validator is stateless and dependency-free — every consumer can
share one instance.
"""
from __future__ import annotations

from enum import Enum
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field

from ..contracts.types import IssueCommandPayload


# Command allowlist — the P2 types plus the P1 instruction-processor
# types. Adding a new type here is the binding contract for "the device
# knows how to execute this" (firmware executor or, for P1, the device
# simulator).
ALLOWLIST: frozenset[str] = frozenset({
    "capture_photo",
    "record_video",
    "start_audio",
    "stop_audio",
    "request_buffer",
    # P1 instruction processor.
    "record_audio",
    "start_video",
    "stop_video",
    "flush_snapshots",
    # P2 power/sleep: low-power mode (no params; reduces power, so always
    # available — no capability requirement in guardrails_command.py).
    "sleep",
    # P3 video: ambient-snapshot cadence config (seconds, 0..600, 0=off).
    # No capability requirement; no battery floor (quick config change).
    "set_snapshot_interval",
})


class RejectionReason(str, Enum):
    """Why the CommandValidator refused a payload.

    Stable wire strings. The Android UI maps each value to a
    user-facing message. Adding a new value is a contract change.
    """

    UNKNOWN_TYPE = "unknown_command_type"
    UNKNOWN_PARAM = "unknown_param"
    MISSING_REQUIRED_PARAM = "missing_required_param"
    INVALID_PARAM_TYPE = "invalid_param_type"
    PARAM_OUT_OF_RANGE = "param_out_of_range"
    INVALID_IDEMPOTENCY_KEY = "invalid_idempotency_key"


# Per-type param schemas. Each entry is:
#   (required_keys, optional_keys, {key: (type_check, min, max)})
#   - type_check: a callable that takes a value and returns True if
#     it's the right type (we don't use isinstance directly so
#     bool/int don't sneak through as numeric).
#   - min/max: optional bounds (inclusive).
def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


_TYPE_SCHEMAS: dict[str, dict] = {
    "capture_photo": {
        "required": (),
        "optional": (),
        "fields": {},
    },
    "record_video": {
        "required": ("duration_s",),
        "optional": (),
        "fields": {
            "duration_s": (_is_number, 1, 30),
        },
    },
    "start_audio": {
        "required": (),
        "optional": (),
        "fields": {},
    },
    "stop_audio": {
        "required": (),
        "optional": (),
        "fields": {},
    },
    "request_buffer": {
        "required": ("seconds",),
        "optional": (),
        "fields": {
            "seconds": (_is_number, 1, 60),
        },
    },
    # P1 instruction processor.
    "record_audio": {
        "required": ("duration_s",),
        "optional": (),
        "fields": {
            "duration_s": (_is_number, 1, 120),
        },
    },
    "start_video": {"required": (), "optional": (), "fields": {}},
    "stop_video": {"required": (), "optional": (), "fields": {}},
    "flush_snapshots": {"required": (), "optional": (), "fields": {}},
    # P2 power/sleep: no params.
    "sleep": {"required": (), "optional": (), "fields": {}},
    # P3 video: ambient-snapshot cadence. seconds in [0, 600]; 0 = off.
    "set_snapshot_interval": {
        "required": ("seconds",),
        "optional": (),
        "fields": {
            "seconds": (_is_number, 0, 600),
        },
    },
}


@runtime_checkable
class CommandValidator(Protocol):
    """Single seam for "is this command valid + safe to dispatch?"."""

    def validate(self, payload: IssueCommandPayload) -> "CommandValidationResult": ...


class ValidatedCommand(BaseModel):
    """A command whose params have passed validation.

    Carries the original payload unchanged (idempotency_key,
    command_type, confidence) plus the *cleaned* params (the input
    params filtered against the schema, with defaults applied). The
    dispatcher turns this into a persisted :class:`Command`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    command_type: str
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    confidence: float = Field(ge=0.0, le=1.0)


class CommandValidationResult(BaseModel):
    """The output of :class:`CommandValidator`.

    ``rejection is None`` means the command is valid; ``command`` carries
    the cleaned payload ready to dispatch. A non-None ``rejection``
    means the command was refused; ``message`` is a user-facing
    explanation. Audit logs carry ``rejection`` verbatim; the UI
    shows ``message`` (possibly translated to the user's locale
    later).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    command: ValidatedCommand | None = None
    rejection: RejectionReason | None = None
    message: str | None = None


class StrictCommandValidator:
    """Default :class:`CommandValidator` — the only implementation
    in this slice.

    Stateless; safe to share across requests. Every consumer
    (Planner, HTTP routes) reads the same instance.
    """

    def validate(self, payload: IssueCommandPayload) -> CommandValidationResult:
        # 1. Empty idempotency_key is a protocol violation.
        if not payload.idempotency_key:
            return CommandValidationResult(
                rejection=RejectionReason.INVALID_IDEMPOTENCY_KEY,
                message="idempotency_key must not be empty",
            )
        # 2. Allowlist check.
        if payload.command_type not in ALLOWLIST:
            return CommandValidationResult(
                rejection=RejectionReason.UNKNOWN_TYPE,
                message=(
                    f"command type {payload.command_type!r} is not in the P2 "
                    f"allowlist; only {sorted(ALLOWLIST)} are autonomously "
                    f"executable"
                ),
            )
        # 3. Per-type param schema.
        schema = _TYPE_SCHEMAS.get(payload.command_type)
        if schema is None:
            # Defensive: should not happen (allowlist is the source of
            # truth). Treat as UNKNOWN_TYPE for the same reason.
            return CommandValidationResult(
                rejection=RejectionReason.UNKNOWN_TYPE,
                message=f"no schema registered for {payload.command_type!r}",
            )
        # 4. Reject unknown keys.
        allowed = set(schema["required"]) | set(schema["optional"])
        for key in payload.params:
            if key not in allowed:
                return CommandValidationResult(
                    rejection=RejectionReason.UNKNOWN_PARAM,
                    message=(
                        f"param {key!r} is not supported for "
                        f"{payload.command_type!r}"
                    ),
                )
        # 5. Check required keys present.
        for key in schema["required"]:
            if key not in payload.params:
                return CommandValidationResult(
                    rejection=RejectionReason.MISSING_REQUIRED_PARAM,
                    message=(
                        f"param {key!r} is required for "
                        f"{payload.command_type!r}"
                    ),
                )
        # 6. Type-check + bound-check each field.
        cleaned: dict[str, Any] = {}
        for key, (type_check, lo, hi) in schema["fields"].items():
            if key not in payload.params:
                continue
            value = payload.params[key]
            if not type_check(value):
                return CommandValidationResult(
                    rejection=RejectionReason.INVALID_PARAM_TYPE,
                    message=(
                        f"param {key!r} must be a number, got "
                        f"{type(value).__name__}"
                    ),
                )
            if lo is not None and value < lo:
                return CommandValidationResult(
                    rejection=RejectionReason.PARAM_OUT_OF_RANGE,
                    message=(
                        f"param {key!r}={value} is below the minimum {lo}"
                    ),
                )
            if hi is not None and value > hi:
                return CommandValidationResult(
                    rejection=RejectionReason.PARAM_OUT_OF_RANGE,
                    message=(
                        f"param {key!r}={value} is above the maximum {hi}"
                    ),
                )
            cleaned[key] = value
        # 7. Build the ValidatedCommand. The original payload's
        # idempotency_key + confidence carry through; the params dict
        # is replaced with the cleaned one (drops unknown keys, but we
        # already rejected them above so this is a no-op here).
        return CommandValidationResult(
            command=ValidatedCommand(
                command_type=payload.command_type,
                params=cleaned,
                idempotency_key=payload.idempotency_key,
                confidence=payload.confidence,
            ),
            rejection=None,
        )
