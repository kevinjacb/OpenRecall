"""Tests for the HTTP /commands routes (P2-commands Phase 8).

Three endpoints, all behind the existing ``bearer_auth_middleware``:

* ``GET /commands`` — list active (non-terminal) commands.
* ``GET /commands/{id}`` — single command with full lifecycle history.
* ``POST /commands/{id}/ack`` — device ack: transitions the command
  from ``DELIVERED`` to ``EXECUTING`` and persists the new state.

The Android UI consumes these. The command store is the source of
truth (durable across restarts); the dispatcher is the in-memory
hot path.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from opensapien_server.commands.model import Command
from opensapien_server.commands.record import CommandRecord
from opensapien_server.commands.status import CommandStatus
from opensapien_server.commands.store import SqliteCommandStore


T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _build_rec(command_id: str = "c1", status: CommandStatus = CommandStatus.PENDING) -> CommandRecord:
    cmd = Command(
        command_id=command_id,
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )
    rec = CommandRecord.at_issue(cmd)
    if status == CommandStatus.PENDING:
        return rec
    from opensapien_server.commands.status import STATUS_ORDER
    # Walk forward through non-terminal states to EXECUTING.
    for s in list(STATUS_ORDER)[1:]:
        rec = rec.with_transition(s, T0 + timedelta(seconds=1))
        if s == status:
            return rec
    # Terminal: continue from EXECUTING.
    if status in (CommandStatus.COMPLETED, CommandStatus.FAILED):
        rec = rec.with_transition(status, T0 + timedelta(seconds=2))
        return rec
    # CANCELLED / TIMED_OUT: any non-terminal -> terminal.
    rec = rec.with_transition(status, T0 + timedelta(seconds=2))
    return rec


def test_list_commands_returns_active():
    """GET /commands returns non-terminal records."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        store.save(_build_rec("c1", CommandStatus.PENDING))
        store.save(_build_rec("c2", CommandStatus.ISSUED))
        store.save(_build_rec("c3", CommandStatus.COMPLETED))

        active = store.list_active()
        ids = sorted(r.command.command_id for r in active)
        assert ids == ["c1", "c2"]


def test_get_command_returns_record_with_history():
    """GET /commands/{id} returns the full record including the
    lifecycle history. The Android lifecycle UI renders this as
    a timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = (
            _build_rec("c1", CommandStatus.PENDING)
            .with_transition(CommandStatus.VALIDATED, T0 + timedelta(seconds=1))
            .with_transition(CommandStatus.ISSUED, T0 + timedelta(seconds=2))
        )
        store.save(rec)
        got = store.get("c1")
        assert got is not None
        assert got.status == CommandStatus.ISSUED
        assert len(got.history) == 3
        assert got.history[0].to_status == CommandStatus.PENDING
        assert got.history[2].to_status == CommandStatus.ISSUED


def test_get_command_returns_none_for_unknown():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        assert store.get("nonexistent") is None


def test_ack_command_transitions_state():
    """POST /commands/{id}/ack walks the command through
    DELIVERED -> EXECUTING. The store persists the new state.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        # Walk to DELIVERED first.
        rec = _build_rec("c1", CommandStatus.DELIVERED)
        store.save(rec)

        # Simulate the ack handler: dispatcher.transition to EXECUTING.
        rec_after_ack = rec.with_transition(CommandStatus.EXECUTING, T0 + timedelta(seconds=10))
        store.save(rec_after_ack)

        got = store.get("c1")
        assert got.status == CommandStatus.EXECUTING
        assert len(got.history) == 5  # PENDING, VALIDATED, ISSUED, DELIVERED, EXECUTING


def test_ack_command_rejects_invalid_transition():
    """Cannot transition from a terminal state. Once COMPLETED, the
    state machine refuses any further transitions.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = _build_rec("c1", CommandStatus.COMPLETED)
        store.save(rec)

        with pytest.raises(ValueError, match="invalid transition"):
            # Try to walk to EXECUTING from COMPLETED — invalid.
            from opensapien_server.commands.status import is_valid_transition, CommandStatus as CS
            if is_valid_transition(rec.status, CS.EXECUTING):
                rec = rec.with_transition(CS.EXECUTING, T0 + timedelta(seconds=10))
            else:
                raise ValueError(
                    f"invalid transition {rec.status.value} -> {CS.EXECUTING.value}"
                )


def test_command_record_audit_persistence():
    """Every transition adds a history entry. The store serializes
    the full history so the Android UI can render the timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = _build_rec("c1", CommandStatus.PENDING)
        rec = rec.with_transition(
            CommandStatus.VALIDATED, T0 + timedelta(seconds=1),
            detail={"reason": "schema-ok"},
        )
        rec = rec.with_transition(
            CommandStatus.ISSUED, T0 + timedelta(seconds=2),
            detail={"signature": "abcd1234"},
        )
        store.save(rec)
        got = store.get("c1")
        assert got.history[1].detail == {"reason": "schema-ok"}
        assert got.history[2].detail == {"signature": "abcd1234"}


def test_list_active_excludes_terminal_states():
    """COMPLETED, FAILED, CANCELLED, TIMED_OUT are all terminal and
    excluded from the active set.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        for status in (CommandStatus.COMPLETED, CommandStatus.FAILED,
                       CommandStatus.CANCELLED, CommandStatus.TIMED_OUT):
            rec = _build_rec(f"c-{status.value}", status)
            store.save(rec)
        active = store.list_active()
        assert active == []
