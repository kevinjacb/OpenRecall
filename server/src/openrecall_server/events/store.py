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

    def last_event(self, session_id: str) -> CaptureEvent | None:
        """The highest-seq event for a session, or None if none.

        Used by the gateway on ``hello`` to continue a reconnected session's
        per-connection ``event_seq`` / ``cum_ms`` past the events already in
        the store. The BLE relay does not replay on reconnect — it resumes at
        the device's current chunk_seq — so a fresh GatewayCore must pick up
        the event counter where the prior connection left off, or the new
        transcripts collide with existing ``event_id``s and are dropped as
        duplicates (``stored=False``), starving extraction.
        """
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

    def relabel_speaker(self, *, from_id: str, to_id: str, session_id: str,
                        scope: str) -> int:
        """Re-label matching events' ``speaker`` from ``from_id`` to ``to_id``
        (manual correction). ``scope`` is carried for forward-compat (v1
        treats every scope as "all of this speaker within the session").
        Returns the number of rows changed."""
        ...

    def search(self, query: str, *, limit: int = 200) -> list[CaptureEvent]:
        """Transcript events whose text contains ``query``, newest first.

        Substring matching, case-insensitive. Backs the "Search transcripts"
        field (spec §2.4), which searches *what was said* — distinct from
        ``/memory?q=``, which does semantic search over extracted atoms and
        returns the wrong object type for a list of recordings.
        """
        ...

    def delete_range(self, session_id: str, first_seq: int, last_seq: int) -> int:
        """Delete events in the inclusive seq range. Returns rows deleted.

        Used by the cascading segment delete (spec §5.2), which is the only
        thing allowed to remove from this otherwise append-only log.
        """
        ...


# The column list every event SELECT shares, paired with `_row_to_event`.
_EVENT_COLUMNS = (
    "event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms, "
    "speaker, speaker_confidence, speaker_assignment"
)


def _row_to_event(r) -> CaptureEvent:
    return CaptureEvent(
        event_id=r[0],
        session_id=r[1],
        seq=r[2],
        kind=r[3],
        created_at=r[4],
        text=r[5],
        duration_ms=r[6],
        start_ms=r[7],
        speaker=r[8],
        speaker_confidence=r[9],
        speaker_assignment=r[10],
    )


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

    def last_event(self, session_id: str) -> CaptureEvent | None:
        events = self._by_session.get(session_id)
        if not events:
            return None
        # Appends are in seq order, but be defensive against any reordering.
        return max(events, key=lambda e: e.seq)

    def sessions(self) -> list[str]:
        return list(self._by_session.keys())

    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        rows = self._by_session.get(session_id, [])
        n = 0
        for i, e in enumerate(rows):
            if e.speaker == from_id:
                rows[i] = e.model_copy(update={
                    "speaker": to_id, "speaker_confidence": 0.9,
                    "speaker_assignment": "confirmed"})
                n += 1
        return n

    def search(self, query: str, *, limit: int = 200) -> list[CaptureEvent]:
        needle = query.strip().lower()
        if not needle:
            return []
        hits = [
            e
            for rows in self._by_session.values()
            for e in rows
            if e.kind == "transcript" and needle in (e.text or "").lower()
        ]
        hits.sort(key=lambda e: (e.created_at, e.session_id, e.seq), reverse=True)
        return hits[:limit]

    def delete_range(self, session_id: str, first_seq: int, last_seq: int) -> int:
        rows = self._by_session.get(session_id)
        if not rows:
            return 0
        keep = [e for e in rows if not (first_seq <= e.seq <= last_seq)]
        removed = len(rows) - len(keep)
        for e in rows:
            if first_seq <= e.seq <= last_seq:
                self._seen.discard(e.event_id)
        self._by_session[session_id] = keep
        return removed


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
                start_ms   INTEGER NOT NULL,
                speaker            TEXT,
                speaker_confidence REAL,
                speaker_assignment  TEXT
            )
            """
        )
        # Idempotent: add the speaker columns to legacy (pre-speaker) databases.
        from ..memory.migrations import migrate_capture_events_table
        migrate_capture_events_table(self._conn)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_events_session_seq "
            "ON capture_events (session_id, seq)"
        )
        self._conn.commit()

    def append(self, event: CaptureEvent) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO capture_events "
                "(event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms, "
                " speaker, speaker_confidence, speaker_assignment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event.event_id,
                    event.session_id,
                    event.seq,
                    event.kind,
                    event.created_at.isoformat(),
                    event.text,
                    event.duration_ms,
                    event.start_ms,
                    event.speaker,
                    event.speaker_confidence,
                    event.speaker_assignment,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1  # 0 when the UNIQUE event_id already existed

    def events(self, session_id: str) -> list[CaptureEvent]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms, "
                " speaker, speaker_confidence, speaker_assignment "
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
                speaker=r[8],
                speaker_confidence=r[9],
                speaker_assignment=r[10],
            )
            for r in rows
        ]

    def last_event(self, session_id: str) -> CaptureEvent | None:
        with self._lock:
            r = self._conn.execute(
                "SELECT event_id, session_id, seq, kind, created_at, text, duration_ms, start_ms, "
                " speaker, speaker_confidence, speaker_assignment "
                "FROM capture_events WHERE session_id = ? ORDER BY seq DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        if r is None:
            return None
        return CaptureEvent(
            event_id=r[0],
            session_id=r[1],
            seq=r[2],
            kind=r[3],
            created_at=r[4],
            text=r[5],
            duration_ms=r[6],
            start_ms=r[7],
            speaker=r[8],
            speaker_confidence=r[9],
            speaker_assignment=r[10],
        )

    def sessions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM capture_events"
            ).fetchall()
        return [r[0] for r in rows]

    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE capture_events SET speaker=?, speaker_confidence=0.9, "
                "speaker_assignment='confirmed' WHERE session_id=? AND speaker=?",
                (to_id, session_id, from_id),
            )
            self._conn.commit()
            return cur.rowcount

    def search(self, query: str, *, limit: int = 200) -> list[CaptureEvent]:
        needle = query.strip().lower()
        if not needle:
            return []
        # `%` and `_` are LIKE wildcards, so a user typing "50%" would
        # otherwise match everything. Escape them (and the escape character
        # itself) and declare the ESCAPE clause.
        escaped = (
            needle.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        )
        # No FTS5 in v1: the table is small, LIKE keeps the in-memory twin
        # trivially equivalent, and a full-text index is a schema commitment.
        # Upgrade path if this gets slow: an FTS5 virtual table on `text`,
        # kept in sync by triggers.
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_EVENT_COLUMNS} FROM capture_events "
                "WHERE kind = 'transcript' AND LOWER(text) LIKE ? ESCAPE '\\' "
                "ORDER BY created_at DESC, session_id DESC, seq DESC LIMIT ?",
                (f"%{escaped}%", limit),
            ).fetchall()
        return [_row_to_event(r) for r in rows]

    def delete_range(self, session_id: str, first_seq: int, last_seq: int) -> int:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM capture_events "
                "WHERE session_id = ? AND seq >= ? AND seq <= ?",
                (session_id, first_seq, last_seq),
            )
            self._conn.commit()
            return cur.rowcount
