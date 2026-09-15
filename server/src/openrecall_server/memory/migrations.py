"""Non-destructive schema migrations for memory tables.

A small, explicit list of ``ALTER TABLE`` additions, idempotent via
``PRAGMA table_info``. Each column ships a ``DEFAULT`` so the migration
backfills existing rows with sensible values and never blocks reads.

The list is append-only: once a column lands here, it stays. Re-running
this module against an already-migrated database is a no-op.
"""
from __future__ import annotations

import sqlite3

# (column_name, column_declaration)
# Each declaration includes a DEFAULT so existing rows get a valid value
# on ALTER TABLE without a follow-up UPDATE.
ADDITIONS: list[tuple[str, str]] = [
    ("extraction_version",       "TEXT NOT NULL DEFAULT 'v1'"),
    ("embedding_model",          "TEXT NOT NULL DEFAULT ''"),
    ("embedding_version",        "INTEGER NOT NULL DEFAULT 0"),
    ("extractor_prompt_version", "TEXT NOT NULL DEFAULT 'v1'"),
    ("source_pipeline_version",  "TEXT NOT NULL DEFAULT 'transcript'"),
]

# Speaker-recognition columns (additive, nullable). Shared by capture_events and
# memory_atoms so the event/atom schemas carry the assigned speaker. NULL means
# no attribution (silence/no-speech hop, or speaker ID disabled).
SPEAKER_ADDITIONS: list[tuple[str, str]] = [
    ("speaker",             "TEXT"),  # UUID, or NULL
    ("speaker_confidence",  "REAL"),  # 0..1, or NULL
    ("speaker_assignment",  "TEXT"),  # "confirmed" | "tentative" | "none", or NULL
]


def migrate_memory_atoms_table(conn: sqlite3.Connection) -> None:
    """Add the five version columns + three speaker columns to ``memory_atoms``.

    Idempotent: each addition checks the existing schema and is skipped if the
    column is already present. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    for name, decl in ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    for name, decl in SPEAKER_ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    conn.commit()


def migrate_memory_atoms_occurred_at(conn: sqlite3.Connection) -> None:
    """Add ``occurred_at`` (conversation time, spec D6) to ``memory_atoms``.

    Nullable rather than ``NOT NULL DEFAULT``: the column's whole point is
    that it can be *unknown* for a row written before it existed, and
    ``MemoryAtom.timeline_at`` is the one place that resolves the fallback
    to ``created_at``. Seeding the column with ``created_at`` at migration
    time instead would make a guess indistinguishable from a real value.

    The index is ``(occurred_at, atom_id)`` — the exact keyset order the
    list endpoint pages on, so a page is an index range scan and the
    tiebreaker never falls back to a sort.

    Idempotent via ``PRAGMA table_info``. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    if "occurred_at" not in existing:
        conn.execute("ALTER TABLE memory_atoms ADD COLUMN occurred_at TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_atoms_occurred "
        "ON memory_atoms (occurred_at, atom_id)"
    )
    conn.commit()


def migrate_memory_index_occurred_at(conn: sqlite3.Connection) -> None:
    """Add ``occurred_at`` (conversation time, spec D6) to ``memory_index``.

    Mirrors :func:`migrate_memory_atoms_occurred_at`: nullable, additive-only,
    idempotent via ``PRAGMA table_info``. No index on the column — unlike
    ``memory_atoms``, ``SqliteMemoryIndex.search`` does not filter or order
    on ``occurred_at`` in SQL (ranking happens in Python), so no companion
    index is needed here.

    Safe to call on every startup; never touches an existing row.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)")}
    if "occurred_at" not in existing:
        conn.execute("ALTER TABLE memory_index ADD COLUMN occurred_at TEXT")
    conn.commit()


def migrate_capture_events_table(conn: sqlite3.Connection) -> None:
    """Add the three speaker columns to ``capture_events`` if absent.

    Idempotent via ``PRAGMA table_info``. Nullable (no ``DEFAULT NOT NULL``) so
    existing rows backfill to NULL. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(capture_events)")}
    for name, decl in SPEAKER_ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE capture_events ADD COLUMN {name} {decl}")
    conn.commit()


def migrate_extraction_cursor_table(conn: sqlite3.Connection) -> None:
    """Add the ``extractor_version`` column to ``extraction_cursor`` if absent.

    The cursor records the last capture-event seq extracted for a session,
    but that progress is only meaningful relative to the extractor that
    produced it. ``extractor_version`` stamps the cursor with the version of
    the extractor that advanced it; when the extraction algorithm or prompt
    changes, the new version mismatches the stamped rows and the worker
    re-extracts the session instead of skipping it.

    Legacy rows (written by pre-versioning code, or by the old per-event
    ``ExtractionPipeline``) backfill to ``'legacy'`` so any versioned
    extractor treats them as stale on the next run. Idempotent and safe to
    call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(extraction_cursor)")}
    if "extractor_version" not in existing:
        conn.execute(
            "ALTER TABLE extraction_cursor ADD COLUMN extractor_version "
            "TEXT NOT NULL DEFAULT 'legacy'"
        )
    conn.commit()
