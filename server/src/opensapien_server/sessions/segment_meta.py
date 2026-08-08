"""Durable per-segment metadata — titles (spec §2.2).

Segments themselves are derived state, rebuilt from the event log on every
start. Titles are not: an LLM call produced them once, and a user may have
renamed one by hand. So they need a home that survives a restart, keyed by
the deterministic ``segment_id`` that a rebuild reproduces exactly.

``title_source`` is the field that keeps the auto-titler honest. A ``"user"``
title is never overwritten — losing someone's own name for a recording to a
background job is the kind of small betrayal that makes a feature untrusted.
"""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

TITLE_MAX_CHARS = 120


@dataclass(frozen=True)
class SegmentMeta:
    segment_id: str
    title: str | None = None
    title_source: str | None = None  # "llm" | "user"
    updated_at: datetime | None = None


@runtime_checkable
class SegmentMetaStore(Protocol):
    def get(self, segment_id: str) -> SegmentMeta | None: ...

    def titles_for(self, segment_ids: list[str]) -> dict[str, str]:
        """Titles for a page of segments in one call, keyed by segment id.

        Batched deliberately: the list endpoint renders a page of rows and a
        per-row lookup would put an N+1 back into the hot read path.
        """
        ...

    def set_title(self, segment_id: str, title: str, *, source: str) -> bool:
        """Store a title. Returns False when the write was declined.

        An ``llm`` write is declined if a ``user`` title already exists; a
        ``user`` write always wins.
        """
        ...

    def delete(self, segment_id: str) -> bool: ...


class InMemorySegmentMetaStore:
    def __init__(self) -> None:
        self._rows: dict[str, SegmentMeta] = {}
        self._lock = threading.Lock()

    def get(self, segment_id: str) -> SegmentMeta | None:
        with self._lock:
            return self._rows.get(segment_id)

    def titles_for(self, segment_ids: list[str]) -> dict[str, str]:
        with self._lock:
            return {
                sid: self._rows[sid].title
                for sid in segment_ids
                if sid in self._rows and self._rows[sid].title
            }

    def set_title(self, segment_id: str, title: str, *, source: str) -> bool:
        with self._lock:
            existing = self._rows.get(segment_id)
            if source == "llm" and existing is not None and existing.title_source == "user":
                return False
            self._rows[segment_id] = SegmentMeta(
                segment_id=segment_id,
                title=title,
                title_source=source,
                updated_at=datetime.now(timezone.utc),
            )
            return True

    def delete(self, segment_id: str) -> bool:
        with self._lock:
            return self._rows.pop(segment_id, None) is not None


class SqliteSegmentMetaStore:
    """Durable :class:`SegmentMetaStore`, following the ``SqliteEventStore``
    template: one shared connection, ``check_same_thread=False``, and a lock,
    because the titling sweep writes from a worker thread while HTTP handlers
    read on the event loop.
    """

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS segment_meta (
                segment_id   TEXT PRIMARY KEY,
                title        TEXT,
                title_source TEXT CHECK(title_source IN ('llm','user')),
                updated_at   TEXT
            );
            """
        )
        self._conn.commit()

    def get(self, segment_id: str) -> SegmentMeta | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT segment_id, title, title_source, updated_at "
                "FROM segment_meta WHERE segment_id = ?",
                (segment_id,),
            ).fetchone()
        if row is None:
            return None
        return SegmentMeta(
            segment_id=row[0],
            title=row[1],
            title_source=row[2],
            updated_at=datetime.fromisoformat(row[3]) if row[3] else None,
        )

    def titles_for(self, segment_ids: list[str]) -> dict[str, str]:
        if not segment_ids:
            return {}
        placeholders = ",".join("?" * len(segment_ids))
        with self._lock:
            rows = self._conn.execute(
                f"SELECT segment_id, title FROM segment_meta "
                f"WHERE segment_id IN ({placeholders}) AND title IS NOT NULL",
                tuple(segment_ids),
            ).fetchall()
        return {r[0]: r[1] for r in rows}

    def set_title(self, segment_id: str, title: str, *, source: str) -> bool:
        with self._lock:
            if source == "llm":
                # Single statement so the "don't clobber a user title" rule
                # is enforced by the database rather than by a read-then-write
                # that another thread can interleave with.
                cur = self._conn.execute(
                    "INSERT INTO segment_meta (segment_id, title, title_source, updated_at) "
                    "VALUES (?, ?, 'llm', ?) "
                    "ON CONFLICT(segment_id) DO UPDATE SET "
                    "  title = excluded.title, "
                    "  title_source = 'llm', "
                    "  updated_at = excluded.updated_at "
                    "WHERE segment_meta.title_source IS NOT 'user'",
                    (segment_id, title, datetime.now(timezone.utc).isoformat()),
                )
            else:
                cur = self._conn.execute(
                    "INSERT OR REPLACE INTO segment_meta "
                    "(segment_id, title, title_source, updated_at) VALUES (?, ?, ?, ?)",
                    (
                        segment_id,
                        title,
                        source,
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            self._conn.commit()
            return cur.rowcount == 1

    def delete(self, segment_id: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "DELETE FROM segment_meta WHERE segment_id = ?", (segment_id,),
            )
            self._conn.commit()
            return cur.rowcount == 1
