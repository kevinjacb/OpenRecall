"""Tests for the SqliteCommandStore (P2-commands Phase 7).

The store persists :class:`CommandRecord` across gateway restarts:
issue-time record + every transition. The dispatcher uses the
in-memory map for hot path; the store is written through to disk.
The audit log carries the lifecycle entries; the command_records
table is the canonical view for the Android command-lifecycle UI.

Three operations:
- save(record): write the record (idempotent on command_id)
- get(command_id): read a single record; None if unknown
- list_active(): records in PENDING / ISSUED / DELIVERED / EXECUTING
  (NOT in a terminal state). Used by the Android UI to populate the
  pending commands list.
"""
from __future__ import annotations

import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sense_server.commands.model import Command
from sense_server.commands.record import CommandRecord, StatusTransition
from sense_server.commands.status import CommandStatus
from sense_server.commands.store import SqliteCommandStore


T0 = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


def _command(command_id: str = "c1") -> Command:
    return Command(
        command_id=command_id,
        session_id="s1",
        type="capture_photo",
        params={},
        issued_at=T0,
        expires_at=T0 + timedelta(seconds=30),
    )


def test_record_persists_across_save_and_get():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = CommandRecord.at_issue(_command("c1"))
        store.save(rec)
        got = store.get("c1")
        assert got is not None
        assert got.command.command_id == "c1"
        assert got.status == CommandStatus.PENDING


def test_record_save_is_idempotent_on_command_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = CommandRecord.at_issue(_command("c1"))
        store.save(rec)
        # Second save of a fresh record with the same command_id.
        # Used by replays / restarts; the second save overwrites with
        # the latest state. The dispatcher mutates the record before
        # each save.
        rec2 = rec.with_transition(CommandStatus.ISSUED, T0 + timedelta(seconds=1))
        store.save(rec2)
        got = store.get("c1")
        assert got.status == CommandStatus.ISSUED
        assert len(got.history) == 2


def test_get_returns_none_for_unknown_command():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        assert store.get("nonexistent") is None


def test_history_round_trips_through_sqlite():
    """Every StatusTransition is preserved in order. The Android UI
    walks the history to render the timeline.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = CommandRecord.at_issue(_command("c1"))
        rec = rec.with_transition(CommandStatus.VALIDATED, T0 + timedelta(seconds=1), detail={"event": "validate"})
        rec = rec.with_transition(CommandStatus.ISSUED, T0 + timedelta(seconds=2), detail={"event": "issue"})
        store.save(rec)
        got = store.get("c1")
        assert got.status == CommandStatus.ISSUED
        assert len(got.history) == 3
        assert got.history[0].to_status == CommandStatus.PENDING
        assert got.history[1].to_status == CommandStatus.VALIDATED
        assert got.history[2].to_status == CommandStatus.ISSUED
        assert got.history[1].detail == {"event": "validate"}


def test_list_active_returns_non_terminal_commands():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)

        # Three commands in different states.
        c1 = CommandRecord.at_issue(_command("c1"))
        c2 = CommandRecord.at_issue(_command("c2")).with_transition(
            CommandStatus.ISSUED, T0 + timedelta(seconds=1)
        )
        c3 = CommandRecord.at_issue(_command("c3")).with_transition(
            CommandStatus.COMPLETED, T0 + timedelta(seconds=1)
        )
        for r in (c1, c2, c3):
            store.save(r)

        active = store.list_active()
        active_ids = sorted(r.command.command_id for r in active)
        assert active_ids == ["c1", "c2"]
        # c3 is COMPLETED — terminal, not in active set.


def test_save_and_get_round_trip_with_unicode_and_special_chars():
    """Params and detail dicts can contain arbitrary JSON-serializable
    values; the store must round-trip them intact.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        cmd = Command(
            command_id="c1",
            session_id="s1",
            type="record_video",
            params={"duration_s": 30, "quality": "high", "tags": ["hello", "world"]},
            issued_at=T0,
            expires_at=T0 + timedelta(seconds=30),
        )
        rec = CommandRecord.at_issue(cmd)
        rec = rec.with_transition(
            CommandStatus.ISSUED,
            T0 + timedelta(seconds=1),
            detail={"reason": "queued", "operator": "kevin", "special": "with \"quotes\" and éçà"},
        )
        store.save(rec)
        got = store.get("c1")
        assert got.command.params == {"duration_s": 30, "quality": "high", "tags": ["hello", "world"]}
        assert got.history[1].detail == {"reason": "queued", "operator": "kevin", "special": "with \"quotes\" and éçà"}


def test_save_persists_across_store_reopen():
    """Gateway restart: close the store, reopen, the record is still
    there. The in-memory state is gone; the SQLite state survives.
    """
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "commands.db"
        store = SqliteCommandStore(path)
        rec = CommandRecord.at_issue(_command("c1")).with_transition(
            CommandStatus.ISSUED, T0 + timedelta(seconds=1)
        )
        store.save(rec)
        # Reopen.
        store2 = SqliteCommandStore(path)
        got = store2.get("c1")
        assert got is not None
        assert got.status == CommandStatus.ISSUED
        assert len(got.history) == 2
