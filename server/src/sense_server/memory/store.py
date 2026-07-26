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
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atom import MemoryAtom
from .migrations import (
    migrate_extraction_cursor_table,
    migrate_memory_atoms_table,
)

_NO_CURSOR = -1  # nothing extracted yet (capture event seqs start at 0)
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
            return list(self._by_session.keys())

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
        self._conn.commit()

    def append(self, atom: MemoryAtom) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO memory_atoms "
                "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
                " extraction_version, embedding_model, embedding_version, "
                " extractor_prompt_version, source_pipeline_version, "
                " speaker, speaker_confidence, speaker_assignment) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    atom.atom_id,
                    atom.session_id,
                    atom.source_event_id,
                    atom.kind,
                    atom.text,
                    atom.created_at.isoformat(),
                    atom.start_ms,
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
                "SELECT atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
                "extraction_version, embedding_model, embedding_version, "
                "extractor_prompt_version, source_pipeline_version, "
                "speaker, speaker_confidence, speaker_assignment "
                "FROM memory_atoms WHERE session_id = ? ORDER BY start_ms",
                (session_id,),
            ).fetchall()
        return [
            MemoryAtom(
                atom_id=r[0],
                session_id=r[1],
                source_event_id=r[2],
                kind=r[3],
                text=r[4],
                created_at=r[5],
                start_ms=r[6],
                extraction_version=r[7],
                embedding_model=r[8],
                embedding_version=r[9],
                extractor_prompt_version=r[10],
                source_pipeline_version=r[11],
                speaker=r[12],
                speaker_confidence=r[13],
                speaker_assignment=r[14],
            )
            for r in rows
        ]

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
        return [r[0] for r in rows]

    def relabel_speaker(self, *, from_id, to_id, session_id, scope):
        with self._lock:
            cur = self._conn.execute(
                "UPDATE memory_atoms SET speaker=?, speaker_confidence=0.9, "
                "speaker_assignment='confirmed' WHERE session_id=? AND speaker=?",
                (to_id, session_id, from_id),
            )
            self._conn.commit()
            return cur.rowcount
