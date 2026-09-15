"""Memory vector index — semantic search over §G atoms.

Stores each atom with its embedding and answers top-k cosine-similarity queries,
scoped per session. Two backends behind one :class:`MemoryIndex` protocol: an
in-memory impl and a durable SQLite impl (vectors stored as JSON, brute-force cosine
in Python). Brute force is exact and dependency-free; a pgvector backend can replace
it for scale while satisfying the same contract.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atom import MemoryAtom
from .migrations import migrate_memory_index_occurred_at

Vector = list[float]


@dataclass(frozen=True, slots=True)
class SearchResult:
    atom: MemoryAtom
    score: float
    vector: list[float] = field(default_factory=list)


def cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity; 0.0 if either vector has zero magnitude."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


@runtime_checkable
class MemoryIndex(Protocol):
    name: str
    version: str

    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        """Index an atom; return True if newly added, False if already present."""
        ...

    def has(self, atom_id: str) -> bool: ...

    def search(self, session_id: str | None, query: Vector, k: int) -> list[SearchResult]:
        """Top-k atoms matching ``query``.

        ``session_id`` filters the search to one session; pass ``None`` for
        a global search across all sessions.
        """
        ...

    def delete_atoms(self, atom_ids: list[str]) -> int:
        """Drop vectors for the given atoms. Returns the number removed.

        Cascades from a segment delete (spec §5.2). Without it a deleted
        memory keeps surfacing in semantic search — the worst possible
        outcome for a delete, since the user is told the thing is gone.
        """
        ...


def _rank(rows: list[tuple[MemoryAtom, Vector]], query: Vector, k: int) -> list[SearchResult]:
    scored = [SearchResult(atom=a, score=cosine(query, v), vector=list(v)) for a, v in rows]
    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:k]


class InMemoryMemoryIndex:
    """In-memory :class:`MemoryIndex` — thread-safe by design.

    Like the atom store, the index is shared mutable state across the
    read path; all reads and writes are guarded by a single lock so
    ranking sees a consistent snapshot.
    """

    name = "in_memory"
    version = "v1"

    def __init__(self) -> None:
        self._entries: dict[str, tuple[MemoryAtom, Vector]] = {}
        self._lock = threading.Lock()

    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        with self._lock:
            if atom.atom_id in self._entries:
                return False
            self._entries[atom.atom_id] = (atom, list(vector))
            return True

    def has(self, atom_id: str) -> bool:
        with self._lock:
            return atom_id in self._entries

    def delete_atoms(self, atom_ids: list[str]) -> int:
        with self._lock:
            return sum(
                1 for aid in atom_ids if self._entries.pop(aid, None) is not None
            )

    def search(self, session_id: str | None, query: Vector, k: int) -> list[SearchResult]:
        with self._lock:
            if session_id is None:
                rows = list(self._entries.values())
            else:
                rows = [
                    (atom, vec)
                    for atom, vec in self._entries.values()
                    if atom.session_id == session_id
                ]
        return _rank(rows, query, k)


class SqliteMemoryIndex:
    name = "sqlite"
    version = "v1"

    def __init__(self, path: str | Path) -> None:
        # The proactive-trigger listener path runs `search` in a
        # fresh thread created by `asyncio.run` inside
        # `ExtractionWorker._dispatch_listeners`'s no-loop fallback
        # (the reconcile-on-start path). Without
        # `check_same_thread=False` + a serialising lock, every
        # cross-thread search raises
        # `ProgrammingError: SQLite objects created in a thread can
        # only be used in that same thread` and the proactive
        # trigger silently fails. This mirrors `SqliteEventStore`
        # and `SqliteAtomStore`.
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS memory_index (
                atom_id         TEXT PRIMARY KEY,
                session_id      TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                kind            TEXT NOT NULL,
                text            TEXT NOT NULL,
                created_at      TEXT NOT NULL,
                start_ms        INTEGER NOT NULL,
                vector          TEXT NOT NULL
            )
            """
        )
        # Conversation time (spec D6). Idempotent; safe to call on every
        # startup. Runs right after CREATE TABLE (which itself is a no-op
        # on an existing DB) so a brand-new table and an existing one both
        # end up on the same schema before anything else touches the file.
        migrate_memory_index_occurred_at(self._conn)
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_index_session ON memory_index (session_id)"
        )
        self._conn.commit()

    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "INSERT OR IGNORE INTO memory_index "
                "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
                " vector, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    atom.atom_id,
                    atom.session_id,
                    atom.source_event_id,
                    atom.kind,
                    atom.text,
                    atom.created_at.isoformat(),
                    atom.start_ms,
                    json.dumps(vector),
                    atom.occurred_at.isoformat() if atom.occurred_at is not None else None,
                ),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def has(self, atom_id: str) -> bool:
        with self._lock:
            row = self._conn.execute(
                "SELECT 1 FROM memory_index WHERE atom_id = ?", (atom_id,)
            ).fetchone()
        return row is not None

    def delete_atoms(self, atom_ids: list[str]) -> int:
        if not atom_ids:
            return 0
        placeholders = ",".join("?" * len(atom_ids))
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM memory_index WHERE atom_id IN ({placeholders})",
                tuple(atom_ids),
            )
            self._conn.commit()
            return cur.rowcount

    def search(self, session_id: str | None, query: Vector, k: int) -> list[SearchResult]:
        with self._lock:
            if session_id is None:
                rows = self._conn.execute(
                    "SELECT atom_id, session_id, source_event_id, kind, text, created_at, "
                    "start_ms, vector, occurred_at FROM memory_index",
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT atom_id, session_id, source_event_id, kind, text, created_at, "
                    "start_ms, vector, occurred_at FROM memory_index WHERE session_id = ?",
                    (session_id,),
                ).fetchall()
        loaded = [
            (
                MemoryAtom(
                    atom_id=r[0],
                    session_id=r[1],
                    source_event_id=r[2],
                    kind=r[3],
                    text=r[4],
                    created_at=r[5],
                    start_ms=r[6],
                    occurred_at=r[8],
                ),
                json.loads(r[7]),
            )
            for r in rows
        ]
        return _rank(loaded, query, k)

    def backfill_occurred_at(self, occurred_by_atom: dict[str, datetime]) -> int:
        """Set ``occurred_at`` on rows that have none, keyed by ``atom_id``.

        Never overwrites a row that already has a value. Returns the number
        of rows actually updated.
        """
        if not occurred_by_atom:
            return 0
        with self._lock:
            cur = self._conn.executemany(
                "UPDATE memory_index SET occurred_at = ? "
                "WHERE atom_id = ? AND occurred_at IS NULL",
                [
                    (when.isoformat(), atom_id)
                    for atom_id, when in occurred_by_atom.items()
                ],
            )
            self._conn.commit()
            return cur.rowcount
