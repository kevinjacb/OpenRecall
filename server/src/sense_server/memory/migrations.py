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


def migrate_memory_atoms_table(conn: sqlite3.Connection) -> None:
    """Add the five version columns to ``memory_atoms`` if absent.

    Idempotent: each addition checks the existing schema and is skipped
    if the column is already present. Safe to call on every startup.
    """
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    for name, decl in ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    conn.commit()
