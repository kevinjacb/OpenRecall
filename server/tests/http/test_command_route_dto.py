"""Tests for the HTTP /commands routes (P2-commands Phase 8).

The route layer is thin: read-only GETs against the
:class:`SqliteCommandStore`, plus an ack endpoint that drives
the dispatcher's lifecycle forward. The routes are a translation
layer between the store's domain (CommandRecord, StatusTransition)
and the Android command-lifecycle UI's wire format.

The store and dispatcher are both injected via ``app[...]`` (the
existing pattern from provisioning / sessions / agent). The store
is the source of truth for the GETs; the dispatcher is the source
of truth for the ack (it runs the state machine, then writes the
result to the store).
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone

import pytest

from sense_server.commands.model import Command
from sense_server.commands.record import CommandRecord, StatusTransition
from sense_server.commands.signing import CommandSigner
from sense_server.commands.status import CommandStatus
from sense_server.commands.store import SqliteCommandStore


# Reuse the response shape from the routes module for round-trip checks.
from sense_server.http.routes.commands import (
    CommandHistoryEntryDto,
    CommandRecordDto,
)


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
    from sense_server.commands.status import STATUS_ORDER
    for s in list(STATUS_ORDER)[1:]:
        rec = rec.with_transition(s, T0 + timedelta(seconds=1))
        if s == status:
            return rec
    if status in (CommandStatus.COMPLETED, CommandStatus.FAILED):
        rec = rec.with_transition(status, T0 + timedelta(seconds=2))
        return rec
    rec = rec.with_transition(status, T0 + timedelta(seconds=2))
    return rec


def test_command_record_dto_round_trips():
    """A CommandRecord → CommandRecordDto → CommandRecord round-trip
    preserves the binding fields (status, type, params, history).
    The wire format is the Android UI's contract; the round-trip
    ensures the mapper doesn't lose data.
    """
    rec = _build_rec("c1", CommandStatus.ISSUED)
    dto = CommandRecordDto.from_record(rec)
    assert dto.command_id == "c1"
    assert dto.type == "capture_photo"
    assert dto.status == "ISSUED"
    assert dto.params == {}
    # History carries the initial PENDING plus the transitions.
    assert len(dto.history) == len(rec.history)
    # round-trip
    rec2 = dto.to_record()
    assert rec2.command.command_id == "c1"
    assert rec2.status == CommandStatus.ISSUED


def test_history_entry_dto_round_trip():
    """Each StatusTransition → CommandHistoryEntryDto → StatusTransition
    preserves from/to status, timestamp, and detail dict.
    """
    from datetime import datetime, timezone
    t = StatusTransition(
        from_status=CommandStatus.PENDING,
        to_status=CommandStatus.ISSUED,
        at=datetime(2026, 7, 1, tzinfo=timezone.utc),
        detail={"reason": "test"},
    )
    dto = CommandHistoryEntryDto.from_transition(t)
    assert dto.from_status == "PENDING"
    assert dto.to_status == "ISSUED"
    assert dto.detail == {"reason": "test"}
    t2 = dto.to_transition()
    assert t2.from_status == CommandStatus.PENDING
    assert t2.to_status == CommandStatus.ISSUED
    assert t2.detail == {"reason": "test"}


def test_status_string_round_trip():
    """The DTO uses string status values (the wire format). The
    mapper converts to/from CommandStatus. Every defined status must
    round-trip cleanly.
    """
    for s in CommandStatus:
        dto = CommandRecordDto.from_record(
            CommandRecord(command=Command(
                command_id="c1", session_id="s1", type="capture_photo",
                params={}, issued_at=T0, expires_at=T0 + timedelta(seconds=30),
            ), status=s, history=())
        )
        assert dto.status == s.value
        rec2 = dto.to_record()
        assert rec2.status == s


def test_list_active_returns_non_terminal():
    """The store's list_active is the binding contract for the
    Android UI's pending commands list.
    """
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        store = SqliteCommandStore(Path(tmp) / "commands.db")
        store.save(_build_rec("c1", CommandStatus.PENDING))
        store.save(_build_rec("c2", CommandStatus.ISSUED))
        store.save(_build_rec("c3", CommandStatus.COMPLETED))
        active = store.list_active()
        ids = sorted(r.command.command_id for r in active)
        assert ids == ["c1", "c2"]


def test_list_active_excludes_terminal():
    """Terminal states are excluded from the active set."""
    with tempfile.TemporaryDirectory() as tmp:
        from pathlib import Path
        store = SqliteCommandStore(Path(tmp) / "commands.db")
        for s in (CommandStatus.COMPLETED, CommandStatus.FAILED,
                   CommandStatus.CANCELLED, CommandStatus.TIMED_OUT):
            store.save(_build_rec(f"c-{s.value}", s))
        assert store.list_active() == []
