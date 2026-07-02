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
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from .atom import MemoryAtom

Vector = list[float]


@dataclass(frozen=True, slots=True)
class SearchResult:
    atom: MemoryAtom
    score: float


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
    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        """Index an atom; return True if newly added, False if already present."""
        ...

    def has(self, atom_id: str) -> bool: ...

    def search(self, session_id: str, query: Vector, k: int) -> list[SearchResult]:
        """Top-k atoms in the session by cosine similarity to ``query``."""
        ...


def _rank(rows: list[tuple[MemoryAtom, Vector]], query: Vector, k: int) -> list[SearchResult]:
    scored = [SearchResult(atom=a, score=cosine(query, v)) for a, v in rows]
    scored.sort(key=lambda r: r.score, reverse=True)
    return scored[:k]


class InMemoryMemoryIndex:
    def __init__(self) -> None:
        self._entries: dict[str, tuple[MemoryAtom, Vector]] = {}

    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        if atom.atom_id in self._entries:
            return False
        self._entries[atom.atom_id] = (atom, list(vector))
        return True

    def has(self, atom_id: str) -> bool:
        return atom_id in self._entries

    def search(self, session_id: str, query: Vector, k: int) -> list[SearchResult]:
        rows = [
            (atom, vec)
            for atom, vec in self._entries.values()
            if atom.session_id == session_id
        ]
        return _rank(rows, query, k)


class SqliteMemoryIndex:
    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path))
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
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_index_session ON memory_index (session_id)"
        )
        self._conn.commit()

    def add(self, atom: MemoryAtom, vector: Vector) -> bool:
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO memory_index "
            "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, vector) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                atom.atom_id,
                atom.session_id,
                atom.source_event_id,
                atom.kind,
                atom.text,
                atom.created_at.isoformat(),
                atom.start_ms,
                json.dumps(vector),
            ),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def has(self, atom_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM memory_index WHERE atom_id = ?", (atom_id,)
        ).fetchone()
        return row is not None

    def search(self, session_id: str, query: Vector, k: int) -> list[SearchResult]:
        rows = self._conn.execute(
            "SELECT atom_id, session_id, source_event_id, kind, text, created_at, "
            "start_ms, vector FROM memory_index WHERE session_id = ?",
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
                ),
                json.loads(r[7]),
            )
            for r in rows
        ]
        return _rank(loaded, query, k)
