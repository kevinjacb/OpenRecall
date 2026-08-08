"""Durable :class:`CommandRecord` store (P2-commands Phase 7).

A SqliteCommandStore is the canonical view of one command for the
Android command-lifecycle UI and the audit log. The in-memory
:class:`CommandDispatcher` is the hot path (signs + dedups); the
store is the persistence layer. On gateway restart, the store's
records are still there; the dispatcher's in-memory map is empty
and gets re-populated as the relay re-issues pending commands.

The schema is intentionally narrow: one row per command with the
current status and a serialized history (JSON list of
:class:`StatusTransition`). The store is the only place that
durability lives; the dispatcher's lifecycle map is ephemeral.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Iterable

from .model import Command
from .record import CommandRecord, StatusTransition
from .status import CommandStatus, terminal_statuses


class SqliteCommandStore:
    """Append-and-overwrite SQLite store for :class:`CommandRecord`.

    Threadsafe (the gateway can write from the worker thread while
    the HTTP route reads from the request thread). Writes are
    idempotent on ``command_id``: a second ``save`` overwrites the
    first with the latest state (the dispatcher calls ``save`` after
    every transition, so the record on disk always reflects the
    current lifecycle).
    """

    def __init__(self, path: str | Path) -> None:
        self._path = str(path)
        self._lock = threading.Lock()
        with sqlite3.connect(self._path) as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS command_records (
                    command_id    TEXT PRIMARY KEY,
                    session_id    TEXT,
                    type          TEXT,
                    params        TEXT,
                    issued_at     TEXT,
                    expires_at    TEXT,
                    status        TEXT,
                    history       TEXT
                );
                CREATE INDEX IF NOT EXISTS ix_command_records_status
                    ON command_records (status);
                """
            )
            conn.commit()

    def save(self, record: CommandRecord) -> None:
        """Persist ``record`` (overwrite on existing command_id)."""
        history_json = json.dumps(
            [
                {
                    "from_status": h.from_status.value if h.from_status else None,
                    "to_status": h.to_status.value,
                    "at": h.at.isoformat(),
                    "detail": h.detail,
                }
                for h in record.history
            ]
        )
        params_json = json.dumps(record.command.params)
        with self._lock, sqlite3.connect(self._path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO command_records "
                "(command_id, session_id, type, params, issued_at, "
                "expires_at, status, history) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.command.command_id,
                    record.command.session_id,
                    record.command.type,
                    params_json,
                    record.command.issued_at.isoformat(),
                    record.command.expires_at.isoformat(),
                    record.status.value,
                    history_json,
                ),
            )
            conn.commit()

    def get(self, command_id: str) -> CommandRecord | None:
        """Read a record by command_id. Returns None if unknown."""
        with self._lock, sqlite3.connect(self._path) as conn:
            row = conn.execute(
                "SELECT command_id, session_id, type, params, issued_at, "
                "expires_at, status, history FROM command_records "
                "WHERE command_id = ?",
                (command_id,),
            ).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def list_active(self) -> list[CommandRecord]:
        """Return records in non-terminal states (PENDING, ISSUED,
        DELIVERED, EXECUTING). The Android UI consumes this for the
        pending-commands list.
        """
        terminal = {s.value for s in terminal_statuses()}
        placeholders = ','.join('?' * len(terminal))
        with self._lock, sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                f"SELECT command_id, session_id, type, params, issued_at, "
                f"expires_at, status, history FROM command_records "
                f"WHERE status NOT IN ({placeholders}) "
                f"ORDER BY issued_at",
                tuple(terminal),
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def list_all(self) -> list[CommandRecord]:
        """All records (for tests + operator debug)."""
        with self._lock, sqlite3.connect(self._path) as conn:
            rows = conn.execute(
                "SELECT command_id, session_id, type, params, issued_at, "
                "expires_at, status, history FROM command_records "
                "ORDER BY issued_at"
            ).fetchall()
        return [self._row_to_record(r) for r in rows]

    def _row_to_record(self, row) -> CommandRecord:
        (cid, session_id, ctype, params_json, issued_at, expires_at,
         status_str, history_json) = row
        from datetime import datetime
        params = json.loads(params_json) if params_json else {}
        history_raw = json.loads(history_json) if history_json else []
        history = tuple(
            StatusTransition(
                from_status=CommandStatus(h["from_status"]) if h["from_status"] else None,
                to_status=CommandStatus(h["to_status"]),
                at=datetime.fromisoformat(h["at"]),
                detail=h["detail"],
            )
            for h in history_raw
        )
        cmd = Command(
            command_id=cid,
            session_id=session_id,
            type=ctype,  # type: ignore[arg-type]
            params=params,
            issued_at=datetime.fromisoformat(issued_at),
            expires_at=datetime.fromisoformat(expires_at),
        )
        return CommandRecord(
            command=cmd,
            status=CommandStatus(status_str),
            history=history,
        )
