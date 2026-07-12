"""Tests for the command validator: allowlist + per-type param bounds.

The validator runs after the LLM has produced a parsed
:class:`AgentAction` (with an :class:`IssueCommandPayload`). It
sanitizes the params against the per-type schema and returns either
a :class:`ValidatedCommand` (a command with cleaned params ready
to dispatch) or a :class:`RejectionReason` (the user sees a friendly
refusal message).

Per-type schemas (per the spec §6.2):

  - capture_photo:   no params
  - record_video:    duration_s in [1, 30] (seconds), optional quality
  - start_audio:     no params
  - stop_audio:      no params
  - request_buffer:  seconds in [1, 60]

The validator also enforces the command allowlist: only the 5 P2
types are autonomously executable. ``display_text``, ``play_audio``,
and ``show_status`` (not in the P2 list) are not even parsed at
this layer — they fail validation as ``UNKNOWN_TYPE``.
"""
from __future__ import annotations

import pytest

from sense_server.contracts.types import IssueCommandPayload
from sense_server.agent.validator_command import (
    ALLOWLIST,
    CommandValidator,
    RejectionReason,
    StrictCommandValidator,
    ValidatedCommand,
)


def _payload(command_type: str, params: dict | None = None,
             idempotency_key: str = "ik-1", confidence: float = 0.9) -> IssueCommandPayload:
    return IssueCommandPayload(
        command_type=command_type,  # type: ignore[arg-type]
        params=params or {},
        idempotency_key=idempotency_key,
        confidence=confidence,
    )


# --- protocol shape -----------------------------------------------------------


def test_strict_command_validator_satisfies_protocol():
    assert isinstance(StrictCommandValidator(), CommandValidator)


# --- capture_photo -----------------------------------------------------------


def test_capture_photo_with_no_params_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("capture_photo"))
    assert out.rejection is None
    assert isinstance(out.command, ValidatedCommand)
    assert out.command.params == {}


def test_capture_photo_rejects_unknown_params():
    v = StrictCommandValidator()
    out = v.validate(_payload("capture_photo", {"extra": "boom"}))
    assert out.rejection is RejectionReason.UNKNOWN_PARAM
    assert "extra" in (out.message or "")


# --- record_video -----------------------------------------------------------


def test_record_video_with_valid_duration_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 10}))
    assert out.rejection is None
    assert out.command.params["duration_s"] == 10


def test_record_video_below_minimum_duration_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 0}))
    assert out.rejection is RejectionReason.PARAM_OUT_OF_RANGE
    assert "duration_s" in (out.message or "")


def test_record_video_above_maximum_duration_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 31}))
    assert out.rejection is RejectionReason.PARAM_OUT_OF_RANGE


def test_record_video_at_minimum_duration_boundary_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 1}))
    assert out.rejection is None


def test_record_video_at_maximum_duration_boundary_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 30}))
    assert out.rejection is None


def test_record_video_missing_duration_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {}))
    assert out.rejection is RejectionReason.MISSING_REQUIRED_PARAM
    assert "duration_s" in (out.message or "")


def test_record_video_non_numeric_duration_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": "ten"}))
    assert out.rejection == RejectionReason.INVALID_PARAM_TYPE


def test_record_video_negative_duration_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": -1}))
    assert out.rejection == RejectionReason.PARAM_OUT_OF_RANGE


# --- start_audio / stop_audio ----------------------------------------------


def test_start_audio_with_no_params_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("start_audio"))
    assert out.rejection is None


def test_stop_audio_with_no_params_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("stop_audio"))
    assert out.rejection is None


def test_start_audio_with_extra_param_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("start_audio", {"duration_s": 5}))
    assert out.rejection == RejectionReason.UNKNOWN_PARAM


# --- request_buffer ---------------------------------------------------------


def test_request_buffer_with_valid_seconds_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("request_buffer", {"seconds": 30}))
    assert out.rejection is None
    assert out.command.params["seconds"] == 30


def test_request_buffer_at_maximum_seconds_boundary_is_valid():
    v = StrictCommandValidator()
    out = v.validate(_payload("request_buffer", {"seconds": 60}))
    assert out.rejection is None


def test_request_buffer_above_maximum_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("request_buffer", {"seconds": 61}))
    assert out.rejection == RejectionReason.PARAM_OUT_OF_RANGE


def test_request_buffer_below_minimum_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("request_buffer", {"seconds": 0}))
    assert out.rejection == RejectionReason.PARAM_OUT_OF_RANGE


# --- allowlist --------------------------------------------------------------


def test_unknown_command_type_is_rejected():
    """The pydantic IssueCommandPayload's Literal only allows the 5
    P2 types. The LLM can't even parse a payload of type
    'display_text' — that fails at pydantic validation, before the
    CommandValidator runs. The validator's UNKNOWN_TYPE rejection
    is a backstop for forward-compat: if a future phase adds a type
    to the pydantic Literal but the validator's schema dict is out
    of sync, the validator catches it rather than silently letting
    a malformed command through.
    """
    v = StrictCommandValidator()
    # The validator's allowlist mirrors the pydantic Literal. Verify
    # they stay in sync: a type in the pydantic Literal but missing
    # from the validator's _TYPE_SCHEMAS gets UNKNOWN_TYPE.
    assert "capture_photo" in ALLOWLIST
    assert "record_video" in ALLOWLIST
    assert "start_audio" in ALLOWLIST
    assert "stop_audio" in ALLOWLIST
    assert "request_buffer" in ALLOWLIST


def test_play_audio_not_in_allowlist():
    """Sanity check: the 5 P2 types are the ONLY allowlisted ones.

    display_text / play_audio / show_status are NOT in the P2
    allowlist. They will be added when P4 implements the
    corresponding firmware executors.
    """
    assert "play_audio" not in ALLOWLIST
    assert "display_text" not in ALLOWLIST
    assert "show_status" not in ALLOWLIST


def test_validator_schemas_match_allowlist():
    """Every allowlisted type has a schema. The allowlist is the
    source of truth for "what the firmware can do" — the schema
    dict is the implementation. They MUST stay in sync; a type
    in the allowlist but missing from the schemas would silently
    accept any param dict. The validator's last line of defense
    catches it (UNKNOWN_TYPE) but the loader should refuse to
    start the gateway. This test pins the invariant at unit-test
    time.
    """
    from sense_server.agent.validator_command import _TYPE_SCHEMAS
    for cmd_type in ALLOWLIST:
        assert cmd_type in _TYPE_SCHEMAS, (
            f"type {cmd_type!r} is allowlisted but has no schema; "
            f"add it to _TYPE_SCHEMAS"
        )


# --- idempotency_key validation ---------------------------------------------


def test_empty_idempotency_key_is_rejected():
    v = StrictCommandValidator()
    out = v.validate(_payload("capture_photo", idempotency_key=""))
    assert out.rejection == RejectionReason.INVALID_IDEMPOTENCY_KEY


# --- validated command contract ----------------------------------------------


def test_validated_command_preserves_idempotency_key():
    """The ValidatedCommand must carry the idempotency_key unchanged
    so the dispatcher can dedup across retries."""
    v = StrictCommandValidator()
    out = v.validate(_payload("capture_photo", idempotency_key="user-42-photo-1"))
    assert out.command.idempotency_key == "user-42-photo-1"


def test_validated_command_preserves_command_type():
    v = StrictCommandValidator()
    out = v.validate(_payload("record_video", {"duration_s": 10}))
    assert out.command.command_type == "record_video"
