"""Idempotent memory-atom stores with a per-session extraction cursor.

Mirrors the event store: one :class:`AtomStore` protocol, an in-memory impl, and a
durable stdlib SQLite impl. Beyond storing atoms (idempotent by ``atom_id``), the
store tracks a per-session **extraction cursor** — the seq of the last capture event
already turned into atoms — so the extraction pass is resumable and exactly-once
even when an event produces no atoms.
"""

from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atom import MemoryAtom
from .migrations import (
    migrate_extraction_cursor_table,
    migrate_memory_atoms_occurred_at,
    migrate_memory_atoms_table,
)

_NO_CURSOR = -1  # nothing extracted yet (capture event seqs start at 0)
_ONE_DAY = timedelta(days=1)
LIST_LIMIT_DEFAULT = 20
LIST_LIMIT_MAX = 100

# SqliteAtomStore stores a sessionless atom (session_id=None, e.g. an unmatched
# vision snapshot) as "" — the memory_atoms table has `session_id TEXT NOT NULL`,
# so NULL would crash. "" satisfies NOT NULL; real session ids are server-
# generated and never empty. Decoded back to None on read.
_SESSIONLESS = ""


@dataclass(frozen=True)
class AtomStats:
    """Aggregate counts for the Memories tab header (spec §1.3).

    ``added_24h`` counts by conversation time (:attr:`MemoryAtom.timeline_at`),
    not extraction time — a batch that extracts last week's sessions today
    must not claim a week of memories were "added today".
    """

    total: int = 0
    added_24h: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)


# The column list every atom SELECT shares, paired with :func:`_row_to_atom`.
# One definition so a new column can never be added to the INSERT and
# silently forgotten by one of the three read paths.
_ATOM_COLUMNS = (
    "atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
    "occurred_at, extraction_version, embedding_model, embedding_version, "
    "extractor_prompt_version, source_pipeline_version, "
    "speaker, speaker_confidence, speaker_assignment"
)

# Conversation time in SQL, with the same fallback as
# :attr:`MemoryAtom.timeline_at`. Rows written before `occurred_at` existed
# must still sort and count somewhere sensible rather than at NULL.
_TIMELINE = "COALESCE(occurred_at, created_at)"


def _row_to_atom(r) -> MemoryAtom:
    return MemoryAtom(
        atom_id=r[0],
        session_id=(None if r[1] == _SESSIONLESS else r[1]),
        source_event_id=r[2],
        kind=r[3],
        text=r[4],
        created_at=r[5],
        start_ms=r[6],
        occurred_at=r[7],
        extraction_version=r[8],
        embedding_model=r[9],
        embedding_version=r[10],
        extractor_prompt_version=r[11],
        source_pipeline_version=r[12],
        speaker=r[13],
        speaker_confidence=r[14],
        speaker_assignment=r[15],
    )


def _as_aware(value: datetime) -> datetime:
    """Coerce a naive datetime to UTC so comparisons never raise.

    SQLite round-trips timestamps as text and pydantic parses an ISO string
    without an offset as naive. Mixing naive and aware datetimes in a
    comparison is a ``TypeError``, which on the list path would surface as a
    500 on a page of perfectly good rows.
    """
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
# Stamp recorded for a cursor written with no version (the legacy / back-compat
# path, e.g. the old per-event ExtractionPipeline, or a pre-versioning DB row
# backfilled by the migration). Any versioned extractor treats 'legacy' as a
# mismatch and re-extracts the session.
_LEGACY_VERSION = "legacy"


@runtime_checkable
class AtomStore(Protocol):
    def append(self, atom: MemoryAtom) -> bool:
        """Append an atom; return True if newly stored, False if a duplicate."""
        ...

    def has(self, atom_id: str) -> bool:
        """Whether an atom with this id is already stored."""
        ...

    def atoms(self, session_id: str) -> list[MemoryAtom]:
        """All atoms for a session, ordered by start_ms."""
        ...

    def iter_atoms(self):
        """Yield every atom in the store (no order guarantee). Used by the
        vision retention sweep, which must scan all scene atoms regardless of
        session. For SQLite this is one unfiltered SELECT; for the in-memory
        store it is the flat list. Returns an iterator; callers may list() it."""
        ...

    def get_cursor(self, session_id: str, *, extractor_version: str | None = None) -> int:
        """Seq of the last capture event extracted for this session (-1 if none).

        When ``extractor_version`` is given, the cursor is only honoured if it
        was stamped with the *same* version; a cursor stamped with a different
        (or legacy) version is treated as ``-1`` so the caller re-extracts the
        session. Passing ``None`` (the default) skips the version check and
        returns the raw last seq — the original contract, preserved for
        callers that don't track versions.
        """
        ...

    def set_cursor(self, session_id: str, seq: int, *, extractor_version: str | None = None) -> None:
        """Record extraction progress for this session.

        ``extractor_version`` stamps the cursor with the version of the
        extractor that advanced it; ``None`` records the ``'legacy'`` stamp
        (back-compat with the old per-event pipeline and pre-versioning code).
        """
        ...

    def sessions(self) -> list[str]:
        """Distinct session ids held by this store. Order is not specified;
        callers that need determinism must sort. Empty if no atoms have been
        appended. Used by the speaker-reassign sweep to relabel every
        session's atoms."""
        ...

    def relabel_speaker(self, *, from_id: str, to_id: str, session_id: str,
                        scope: str) -> int:
        """Re-label matching atoms' ``speaker`` from ``from_id`` to ``to_id``
        (manual correction). Returns the number of rows changed."""
        ...

    def list(
        self,
        *,
        kind: str | None = None,
        session_id: str | None = None,
        limit: int = LIST_LIMIT_DEFAULT,
        before: tuple[datetime, str] | None = None,
    ) -> tuple[list[MemoryAtom], tuple[datetime, str] | None]:
        """A page of atoms newest-first by conversation time (spec §1.2).

        Ordered ``timeline_at DESC, atom_id DESC``. ``before`` is the keyset
        anchor from a prior page — the returned page starts strictly after it.
        Returns ``(atoms, next_anchor)``; ``next_anchor`` is ``None`` on the
        final page.

        ``kind`` matches the **raw stored kind** exactly, with no validation:
        the kind vocabulary is whatever the extractor emitted, and
        :meth:`stats` is how a client discovers which kinds exist.
        """
        ...

    def stats(self, *, now: datetime | None = None) -> AtomStats:
        """Total / last-24h / per-kind counts, by conversation time."""
        ...

    def delete_range(self, session_id: str, start_ms: int, end_ms: int) -> list[str]:
        """Delete atoms whose ``start_ms`` falls in ``[start_ms, end_ms)``.

        Returns the deleted ``atom_id``s so the caller can cascade to the
        vector index — an orphaned vector would keep surfacing a deleted
        memory in search results, which is the worst possible outcome for a
        delete (spec §5.2).
        """
        ...

    def backfill_occurred_at(self, occurred_by_event: dict[str, datetime]) -> int:
        """Set ``occurred_at`` on rows that have none, keyed by
        ``source_event_id``. Returns the number of rows updated. Never
        overwrites a value that is already set."""
        ...


class InMemoryAtomStore:
    """In-memory :class:`AtomStore` — thread-safe by design.

    The store is shared mutable state across the read path (Planner thread,
    extraction worker thread, gateway callbacks). All mutations are guarded
    by ``self._lock`` so concurrent appends and cursor writes never lose
    updates; reads are also synchronized to get a consistent view.
    """

    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._by_session: dict[str, list[MemoryAtom]] = {}
        # session_id -> (last_seq, extractor_version)
        self._cursor: dict[str, tuple[int, str]] = {}
        self._lock = threading.Lock()

    def append(self, atom: MemoryAtom) -> bool:
        with self._lock:
            if atom.atom_id in self._seen:
                return False
            self._seen.add(atom.atom_id)
            self._by_session.setdefault(atom.session_id, []).append(atom)
            return True

    def has(self, atom_id: str) -> bool:
        with self._lock:
            return atom_id in self._seen

    def atoms(self, session_id: str) -> list[MemoryAtom]:
        with self._lock:
            return sorted(self._by_session.get(session_id, []), key=lambda a: a.start_ms)

    def iter_atoms(self):
        return iter(self._all())

    def get_cursor(self, session_id: str, *, extractor_version: str | None = None) -> int:
        with self._lock:
            stamped = self._cursor.get(session_id)
        if stamped is None:
            return _NO_CURSOR
        last_seq, recorded_version = stamped
        if extractor_version is None or recorded_version == extractor_version:
            return last_seq
        # Version mismatch: the cursor was advanced by a different extractor.
        # Treat as unextracted so the caller re-processes the session.
        return _NO_CURSOR

    def set_cursor(self, session_id: str, seq: int, *, extractor_version: str | None = None) -> None:
        with self._lock:
            self._cursor[session_id] = (seq, extractor_version or _LEGACY_VERSION)

    def sessions(self) -> list[str]:
        with self._lock:
            return [s for s in self._by_session.keys() if s is not None]

    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        rows = self._by_session.get(session_id, [])
        n = 0
        for i, a in enumerate(rows):
            if a.speaker == from_id:
                rows[i] = a.model_copy(update={
                    "speaker": to_id, "speaker_confidence": 0.9,
                    "speaker_assignment": "confirmed"})
                n += 1
        return n

    def _all(self) -> list[MemoryAtom]:
        with self._lock:
            return [a for rows in self._by_session.values() for a in rows]

    def list(self, *, kind=None, session_id=None, limit=LIST_LIMIT_DEFAULT, before=None):
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        rows = self._all()
        if kind is not None:
            rows = [a for a in rows if a.kind == kind]
        if session_id is not None:
            rows = [a for a in rows if a.session_id == session_id]
        rows.sort(key=lambda a: (_as_aware(a.timeline_at), a.atom_id), reverse=True)
        if before is not None:
            anchor = (_as_aware(before[0]), before[1])
            rows = [a for a in rows if (_as_aware(a.timeline_at), a.atom_id) < anchor]
        page = rows[:limit]
        if len(rows) > limit and page:
            last = page[-1]
            return page, (_as_aware(last.timeline_at), last.atom_id)
        return page, None

    def stats(self, *, now=None):
        rows = self._all()
        cutoff = _as_aware(now or datetime.now(timezone.utc)) - _ONE_DAY
        by_kind: dict[str, int] = {}
        added = 0
        for a in rows:
            by_kind[a.kind] = by_kind.get(a.kind, 0) + 1
            if _as_aware(a.timeline_at) > cutoff:
                added += 1
        return AtomStats(total=len(rows), added_24h=added, by_kind=by_kind)

    def delete_range(self, session_id, start_ms, end_ms):
        with self._lock:
            rows = self._by_session.get(session_id, [])
            doomed = [a for a in rows if start_ms <= a.start_ms < end_ms]
            if not doomed:
                return []
            self._by_session[session_id] = [a for a in rows if a not in doomed]
            for a in doomed:
                self._seen.discard(a.atom_id)
            return [a.atom_id for a in doomed]

    def backfill_occurred_at(self, occurred_by_event):
        n = 0
        with self._lock:
            for rows in self._by_session.values():
                for i, a in enumerate(rows):
                    if a.occurred_at is not None:
                        continue
                    stamp = occurred_by_event.get(a.source_event_id)
                    if stamp is None:
                        continue
                    rows[i] = a.model_copy(update={"occurred_at": stamp})
                    n += 1
        return n


class SqliteAtomStore:
    def __init__(self, path: str | Path) -> None:
        # The extraction worker writes atoms from a thread pool
        # (`asyncio.to_thread` inside `ExtractionWorker._run` and the
        # reconcile-on-start sweep in `start()`). Without
        # `check_same_thread=False` and a serialising lock, every
        # cross-thread write raises
        # `ProgrammingError: SQLite objects created in a thread can
        # only be used in that same thread` and is silently swallowed
        # by `process_session`'s try/except — the cursor never
        # advances, the atom is never persisted, the live extraction
        # path is dead in production. This mirrors
        # :class:`SqliteEventStore`.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_atoms (
                atom_id         TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                kind            TEXT NOT NULL,
                text            TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                start_ms        INTEGER NOT NULL,
                speaker             TEXT,
                speaker_confidence  REAL,
                speaker_assignment  TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_atoms_session_start
                ON memory_atoms (session_id, start_ms);
            CREATE TABLE IF NOT EXISTS extraction_cursor (
                session_id        TEXT PRIMARY KEY,
                last_seq          INTEGER NOT NULL,
                extractor_version TEXT NOT NULL DEFAULT 'legacy'
            );
            """
        )
        # Backfill the five version columns on legacy (pre-v1) databases.
        # Idempotent; safe to call on every startup.
        migrate_memory_atoms_table(self._conn)
        # Backfill the cursor version column on pre-versioning databases.
        # Idempotent; safe to call on every startup.
        migrate_extraction_cursor_table(self._conn)
        # Conversation time (spec D6) + the keyset index the list endpoint
        # pages on. Idempotent; safe to call on every startup.
        migrate_memory_atoms_occurred_at(self._conn)
        self._conn.commit()

    def append(self, atom: MemoryAtom) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO memory_atoms "
                "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
                " occurred_at, "
                " extraction_version, embedding_model, embedding_version, "
                " extractor_prompt_version, source_pipeline_version, "
                " speaker, speaker_confidence, speaker_assignment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    atom.atom_id,
                    atom.session_id if atom.session_id is not None else _SESSIONLESS,
                    atom.source_event_id,
                    atom.kind,
                    atom.text,
                    atom.created_at.isoformat(),
                    atom.start_ms,
                    atom.occurred_at.isoformat() if atom.occurred_at else None,
                    atom.extraction_version,
                    atom.embedding_model,
                    atom.embedding_version,
                    atom.extractor_prompt_version,
                    atom.source_pipeline_version,
                    atom.speaker,
                    atom.speaker_confidence,
                    atom.speaker_assignment,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def has(self, atom_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM memory_atoms WHERE atom_id = ?", (atom_id,)
            ).fetchone()
        return row is not None

    def atoms(self, session_id: str) -> list[MemoryAtom]:
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ATOM_COLUMNS} FROM memory_atoms "
                "WHERE session_id = ? ORDER BY start_ms",
                (session_id,),
            ).fetchall()
        return [_row_to_atom(r) for r in rows]

    def iter_atoms(self):
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ATOM_COLUMNS} FROM memory_atoms ORDER BY start_ms"
            ).fetchall()
        return iter([_row_to_atom(r) for r in rows])

    def list(self, *, kind=None, session_id=None, limit=LIST_LIMIT_DEFAULT, before=None):
        if limit <= 0:
            raise ValueError(f"limit must be positive, got {limit}")
        where = []
        params: list = []
        if kind is not None:
            where.append("kind = ?")
            params.append(kind)
        if session_id is not None:
            where.append("session_id = ?")
            params.append(session_id)
        if before is not None:
            # Strict keyset: resume after the anchor row, in the same
            # (timeline, id) order the ORDER BY uses. A row-value comparison
            # keeps this a single index range scan.
            where.append(f"({_TIMELINE}, atom_id) < (?, ?)")
            params.extend([_as_aware(before[0]).isoformat(), before[1]])
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        # Over-fetch by one to learn whether another page exists without a
        # second COUNT query.
        params.append(limit + 1)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT {_ATOM_COLUMNS} FROM memory_atoms {clause} "
                f"ORDER BY {_TIMELINE} DESC, atom_id DESC LIMIT ?",
                tuple(params),
            ).fetchall()
        atoms = [_row_to_atom(r) for r in rows[:limit]]
        if len(rows) > limit and atoms:
            last = atoms[-1]
            return atoms, (_as_aware(last.timeline_at), last.atom_id)
        return atoms, None

    def stats(self, *, now=None):
        cutoff = (_as_aware(now or datetime.now(timezone.utc)) - _ONE_DAY).isoformat()
        with self._lock:
            by_kind = {
                r[0]: r[1]
                for r in self._conn.execute(
                    "SELECT kind, COUNT(*) FROM memory_atoms GROUP BY kind"
                ).fetchall()
            }
            added = self._conn.execute(
                f"SELECT COUNT(*) FROM memory_atoms WHERE {_TIMELINE} > ?",
                (cutoff,),
            ).fetchone()[0]
        return AtomStats(total=sum(by_kind.values()), added_24h=added, by_kind=by_kind)

    def delete_range(self, session_id, start_ms, end_ms):
        with self._lock:
            rows = self._conn.execute(
                "SELECT atom_id FROM memory_atoms "
                "WHERE session_id = ? AND start_ms >= ? AND start_ms < ?",
                (session_id, start_ms, end_ms),
            ).fetchall()
            if not rows:
                return []
            self._conn.execute(
                "DELETE FROM memory_atoms "
                "WHERE session_id = ? AND start_ms >= ? AND start_ms < ?",
                (session_id, start_ms, end_ms),
            )
            self._conn.commit()
            return [r[0] for r in rows]

    def backfill_occurred_at(self, occurred_by_event):
        if not occurred_by_event:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "UPDATE memory_atoms SET occurred_at = ? "
                "WHERE source_event_id = ? AND occurred_at IS NULL",
                [
                    (_as_aware(stamp).isoformat(), event_id)
                    for event_id, stamp in occurred_by_event.items()
                ],
            )
            self._conn.commit()
            return cur.rowcount

    def get_cursor(self, session_id: str, *, extractor_version: str | None = None) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT last_seq, extractor_version FROM extraction_cursor WHERE session_id = ?",
                (session_id,),
            ).fetchone()
        if row is None:
            return _NO_CURSOR
        last_seq, recorded_version = row
        if extractor_version is None or recorded_version == extractor_version:
            return last_seq
        # Version mismatch: the cursor was advanced by a different extractor.
        # Treat as unextracted so the caller re-processes the session.
        return _NO_CURSOR

    def set_cursor(self, session_id: str, seq: int, *, extractor_version: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO extraction_cursor "
                "(session_id, last_seq, extractor_version) VALUES (?, ?, ?)",
                (session_id, seq, extractor_version or _LEGACY_VERSION),
            )
            self._conn.commit()

    def sessions(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT session_id FROM memory_atoms"
            ).fetchall()
        return [r[0] for r in rows if r[0] != _SESSIONLESS]

    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory_atoms SET speaker=?, speaker_confidence=0.9, "
                "speaker_assignment='confirmed' WHERE session_id=? AND speaker=?",
                (to_id, session_id, from_id),
            )
            self._conn.commit()
            return cur.rowcount
