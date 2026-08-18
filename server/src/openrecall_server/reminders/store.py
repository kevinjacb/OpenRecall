"""Reminder schedule store — the side table for `kind="reminder"` atoms.

A reminder is a `MemoryAtom(kind="reminder")` in the existing AtomStore
(the atom carries the text; the /memory surfaces see it as a kind bucket
for free) PLUS a row here carrying the schedule (due_at, status, fired_at).
The atom_id is the shared key.

Two implementations behind one :class:`ReminderStore` Protocol: an
in-memory impl for tests and a durable stdlib SQLite impl for production.
Both are thread-safe (the sweeper, the HTTP route, and the planner all
touch the store). The SQLite twin uses ``check_same_thread=False`` +
a serialising lock — the same pattern as SqliteEventStore / SqliteAtomStore.
"""
from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict


class Reminder(BaseModel):
    model_config = ConfigDict(frozen=True)

    atom_id: str
    session_id: str
    text: str
    due_at: datetime
    status: Literal["pending", "fired", "done"] = "pending"
    fired_at: datetime | None = None


@runtime_checkable
class ReminderStore(Protocol):
    def add(self, *, atom_id: str, session_id: str, text: str, due_at: datetime) -> bool: ...
    def due(self, now: datetime) -> list[Reminder]: ...
    def mark_fired(self, atom_id: str, *, fired_at: datetime) -> None: ...
    def mark_done(self, atom_id: str) -> bool: ...
    def list(self, *, only_pending: bool = True) -> list[Reminder]: ...
    def get(self, atom_id: str) -> Reminder | None: ...


def _as_aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


class InMemoryReminderStore:
    def __init__(self) -> None:
        self._by_id: dict[str, Reminder] = {}
        self._lock = threading.Lock()

    def add(self, *, atom_id, session_id, text, due_at) -> bool:
        with self._lock:
            if atom_id in self._by_id:
                return False
            self._by_id[atom_id] = Reminder(
                atom_id=atom_id, session_id=session_id, text=text, due_at=_as_aware(due_at),
            )
            return True

    def due(self, now: datetime) -> list[Reminder]:
        now = _as_aware(now)
        with self._lock:
            rows = [
                r for r in self._by_id.values()
                if r.status == "pending" and _as_aware(r.due_at) <= now
            ]
        # Sort ascending by due_at to match the SQLite twin's `ORDER BY due_at`.
        # Dict insertion order would otherwise leak through (T9's sweeper fires
        # in the order due() returns).
        return sorted(rows, key=lambda r: r.due_at)

    def mark_fired(self, atom_id: str, *, fired_at: datetime) -> None:
        with self._lock:
            r = self._by_id.get(atom_id)
            if r is None or r.status != "pending":
                return
            self._by_id[atom_id] = r.model_copy(
                update={"status": "fired", "fired_at": _as_aware(fired_at)}
            )

    def mark_done(self, atom_id: str) -> bool:
        with self._lock:
            r = self._by_id.get(atom_id)
            if r is None:
                return False
            self._by_id[atom_id] = r.model_copy(update={"status": "done"})
            return True

    def list(self, *, only_pending: bool = True) -> list[Reminder]:
        with self._lock:
            rows = [
                r for r in self._by_id.values()
                if not only_pending or r.status == "pending"
            ]
        # Sort ascending by due_at to match the SQLite twin's `ORDER BY due_at`.
        return sorted(rows, key=lambda r: r.due_at)

    def get(self, atom_id: str) -> Reminder | None:
        with self._lock:
            return self._by_id.get(atom_id)


_COLUMNS = "atom_id, session_id, text, due_at, status, fired_at"


def _row_to_reminder(r) -> Reminder:
    return Reminder(
        atom_id=r[0], session_id=r[1], text=r[2], due_at=_as_aware(datetime.fromisoformat(r[3])),
        status=r[4], fired_at=datetime.fromisoformat(r[5]) if r[5] else None,
    )


class SqliteReminderStore:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS reminders (
                atom_id   TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                text      TEXT NOT NULL,
                due_at    TEXT NOT NULL,
                status    TEXT NOT NULL DEFAULT 'pending',
                fired_at  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_reminders_due
                ON reminders (due_at) WHERE status = 'pending';
            """
        )
        self._conn.commit()

    def add(self, *, atom_id, session_id, text, due_at) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO reminders "
                "(atom_id, session_id, text, due_at, status, fired_at) "
                "VALUES (?, ?, ?, ?, 'pending', NULL)",
                (atom_id, session_id, text, _as_aware(due_at).isoformat()),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def due(self, now: datetime) -> list[Reminder]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_COLUMNS} FROM reminders "
                "WHERE status = 'pending' AND due_at <= ? ORDER BY due_at",
                (_as_aware(now).isoformat(),),
            ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def mark_fired(self, atom_id: str, *, fired_at: datetime) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE reminders SET status = 'fired', fired_at = ? "
                "WHERE atom_id = ? AND status = 'pending'",
                (_as_aware(fired_at).isoformat(), atom_id),
            )
            self._conn.commit()

    def mark_done(self, atom_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE reminders SET status = 'done' WHERE atom_id = ?",
                (atom_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def list(self, *, only_pending: bool = True) -> list[Reminder]:
        with self._lock:
            if only_pending:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM reminders WHERE status = 'pending' ORDER BY due_at"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT {_COLUMNS} FROM reminders ORDER BY due_at"
                ).fetchall()
        return [_row_to_reminder(r) for r in rows]

    def get(self, atom_id: str) -> Reminder | None:
        with self._lock:
            r = self._conn.execute(
                f"SELECT {_COLUMNS} FROM reminders WHERE atom_id = ?", (atom_id,)
            ).fetchone()
        return _row_to_reminder(r) if r else None