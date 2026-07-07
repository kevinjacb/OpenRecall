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
from .migrations import migrate_memory_atoms_table

_NO_CURSOR = -1  # nothing extracted yet (capture event seqs start at 0)


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

    def get_cursor(self, session_id: str) -> int:
        """Seq of the last capture event extracted for this session (-1 if none)."""
        ...

    def set_cursor(self, session_id: str, seq: int) -> None:
        """Record extraction progress for this session."""
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
        self._cursor: dict[str, int] = {}
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

    def get_cursor(self, session_id: str) -> int:
        with self._lock:
            return self._cursor.get(session_id, _NO_CURSOR)

    def set_cursor(self, session_id: str, seq: int) -> None:
        with self._lock:
            self._cursor[session_id] = seq


class SqliteAtomStore:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS memory_atoms (
                atom_id         TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                kind            TEXT NOT NULL,
                text            TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                start_ms        INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_atoms_session_start
                ON memory_atoms (session_id, start_ms);
            CREATE TABLE IF NOT EXISTS extraction_cursor (
                session_id TEXT PRIMARY KEY,
                last_seq   INTEGER NOT NULL
            );
            """
        )
        # Backfill the five version columns on legacy (pre-v1) databases.
        # Idempotent; safe to call on every startup.
        migrate_memory_atoms_table(self._conn)
        self._conn.commit()

    def append(self, atom: MemoryAtom) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO memory_atoms "
            "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
            " extraction_version, embedding_model, embedding_version, "
            " extractor_prompt_version, source_pipeline_version) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def has(self, atom_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memory_atoms WHERE atom_id = ?", (atom_id,)
        ).fetchone()
        return row is not None

    def atoms(self, session_id: str) -> list[MemoryAtom]:
        rows = self._conn.execute(
            "SELECT atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
            "extraction_version, embedding_model, embedding_version, "
            "extractor_prompt_version, source_pipeline_version "
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
            )
            for r in rows
        ]

    def get_cursor(self, session_id: str) -> int:
        row = self._conn.execute(
            "SELECT last_seq FROM extraction_cursor WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        return row[0] if row is not None else _NO_CURSOR

    def set_cursor(self, session_id: str, seq: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO extraction_cursor (session_id, last_seq) VALUES (?, ?)",
            (session_id, seq),
        )
        self._conn.commit()
