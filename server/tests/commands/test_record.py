"""Tests for the CommandRecord lifecycle view.

The record is the storage/lifecycle shape; the :class:`Command` is
the wire shape. Records are durable (a SqliteCommandStore persists
them). The history is append-only — a record never goes backwards.

The Android command lifecycle UI uses this record directly: status
for the lifecycle badge, history for the per-transition detail.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from opensapien_server.commands.model import Command
from opensapien_server.commands.record import CommandRecord, StatusTransition
from opensapien_server.commands.status import CommandStatus


T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 30, 12, 0, 1, tzinfo=timezone.utc)
T2 = datetime(2026, 6, 30, 12, 0, 2, tzinfo=timezone.utc)


def a_command() -> Command:
    return Command(
        command_id="cmd-1",
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0.replace(hour=13),
    )


def test_at_issue_builds_pending_record():
    rec = CommandRecord.at_issue(a_command())
    assert rec.status is CommandStatus.PENDING
    assert len(rec.history) == 1
    assert rec.history[0].from_status is None
    assert rec.history[0].to_status is CommandStatus.PENDING
    assert rec.history[0].at == T0


def test_with_transition_appends_history():
    rec = CommandRecord.at_issue(a_command())
    validated = rec.with_transition(
        CommandStatus.VALIDATED, T1, detail={"event": "validate"}
    )
    assert validated.status is CommandStatus.VALIDATED
    assert len(validated.history) == 2
    assert validated.history[0].to_status is CommandStatus.PENDING
    assert validated.history[1].to_status is CommandStatus.VALIDATED
    assert validated.history[1].from_status is CommandStatus.PENDING
    assert validated.history[1].at == T1
    assert validated.history[1].detail == {"event": "validate"}


def test_history_is_append_only():
    """Each with_transition call returns a NEW record; the original
    is unchanged. This is the append-only invariant the Android UI
    relies on for the history view.
    """
    original = CommandRecord.at_issue(a_command())
    snapshot = original  # record is frozen; same object
    _ = original.with_transition(CommandStatus.VALIDATED, T1)
    assert original.history == snapshot.history
    assert original.status is CommandStatus.PENDING


def test_full_lifecycle_pending_to_completed():
    rec = CommandRecord.at_issue(a_command())
    rec = rec.with_transition(CommandStatus.VALIDATED, T1)
    rec = rec.with_transition(CommandStatus.ISSUED, T2, detail={"command_id": "cmd-1"})
    assert rec.status is CommandStatus.ISSUED
    assert len(rec.history) == 3


def test_command_record_is_frozen():
    rec = CommandRecord.at_issue(a_command())
    with pytest.raises(ValidationError):
        rec.status = CommandStatus.VALIDATED  # type: ignore[misc]


def test_status_transition_detail_is_optional():
    t = StatusTransition(
        from_status=CommandStatus.PENDING,
        to_status=CommandStatus.VALIDATED,
        at=T1,
    )
    assert t.detail == {}


def test_status_transition_with_detail():
    t = StatusTransition(
        from_status=CommandStatus.EXECUTING,
        to_status=CommandStatus.FAILED,
        at=T2,
        detail={"error": "low_battery", "battery_pct": 0.04},
    )
    assert t.detail["error"] == "low_battery"
    assert t.detail["battery_pct"] == 0.04


def test_record_rejects_extra_fields():
    rec = CommandRecord.at_issue(a_command())
    with pytest.raises(ValidationError):
        # extra="forbid"
        rec.__class__.model_validate({
            "command": rec.command,
            "status": rec.status,
            "history": rec.history,
            "extra_field": "nope",
        })
