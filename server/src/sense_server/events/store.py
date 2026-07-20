"""Append-only, idempotent capture-event stores.

Two implementations behind one :class:`EventStore` protocol:

* :class:`InMemoryEventStore` — for tests and ephemeral use.
* :class:`SqliteEventStore` — durable, stdlib-only, idempotent via a UNIQUE
  ``event_id`` constraint and ``INSERT OR IGNORE``. The pgvector/Postgres backend
  for vector retrieval comes later; durable event capture does not need it.

Both dedupe by ``event_id`` and return events per session ordered by ``seq``.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import CaptureEvent


@runtime_checkable
class EventStore(Protocol):
    def append(self, event: CaptureEvent) -> bool:
        """Append an event; return True if newly stored, False if a duplicate."""
        ...

    def events(self, session_id: str) -> list[CaptureEvent]:
        """All events for a session, ordered by seq."""
        ...

    def sessions(self) -> list[str]:
        """Distinct session ids held by this store. Order is not specified;
        callers that need determinism must sort. Empty if no events have
        been appended. Used by the extraction worker's reconcile pass to
        discover historical sessions that the live enqueuer missed
        (e.g. when the gateway was down for a window and the enqueuer
        overflowed, or on the very first start before any live event
        has flowed)."""
        ...


class InMemoryEventStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._by_session: dict[str, list[CaptureEvent]] = {}

    def append(self, event: CaptureEvent) -> bool:
        if event.event_id in self._seen:
            return False
        self._seen.add(event.event_id)
        self._by_session.setdefault(event.session_id, []).append(event)
        return True

    def events(self, session_id: str) -> list[CaptureEvent]:
        return sorted(self._by_session.get(session_id, []), key=lambda e: e.seq)

    def sessions(self) -> list[str]:
        return list(self._by_session.keys())


class SqliteEventStore:
    # The gateway offloads ingest to worker threads, so the connection is shared
    # across threads (check_same_thread=False) and a lock serialises every access —
    # a single sqlite3 connection is not safe for concurrent use.
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS capture_events (
                event_id   TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                seq        INTEGER NOT NULL,
                kind       TEXT NOT NULL,
                created_at TEXT NOT NULL,
                text       TEXT NOT NULL,
                duration_ms INTEGER NOT NULL,
                start_ms   INTEGER NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_session_seq "
            "ON capture_events (session_id, seq)"
        )
        self._conn.commit()

    def append(self, event: CaptureEvent) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO capture_events "
                "(event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.session_id,
                    event.seq,
                    event.kind,
                    event.created_at.isoformat(),
                    event.text,
                    event.duration_ms,
                    event.start_ms,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1  # 0 when the UNIQUE event_id already existed

    def events(self, session_id: str) -> list[CaptureEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms "
                "FROM capture_events WHERE session_id = ? ORDER BY seq",
                (session_id,),
            ).fetchall()
        return [
            CaptureEvent(
                event_id=r[0],
                session_id=r[1],
                seq=r[2],
                kind=r[3],
                created_at=r[4],
                text=r[5],
                duration_ms=r[6],
                start_ms=r[7],
            )
            for r in rows
        ]

    def sessions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM capture_events"
            ).fetchall()
        return [r[0] for r in rows]
