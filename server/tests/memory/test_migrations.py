"""Tests for the non-destructive version-columns migration on memory_atoms."""
from __future__ import annotations

import json
import sqlite3

from openrecall_server.memory.index import SqliteMemoryIndex
from openrecall_server.memory.migrations import (
    migrate_capture_events_table,
    migrate_memory_atoms_table,
)


def _open_legacy_db() -> sqlite3.Connection:
    """A 7-column pre-v1 memory_atoms table — the shape before this slice."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE memory_atoms (
            atom_id         TEXT PRIMARY KEY,
            session_id      TEXT NOT NULL,
            source_event_id TEXT NOT NULL,
            kind            TEXT NOT NULL,
            text            TEXT NOT NULL,
            created_at      TEXT NOT NULL,
            start_ms        INTEGER NOT NULL
        );
        """
    )
    conn.execute(
        "INSERT INTO memory_atoms VALUES (?,?,?,?,?,?,?)",
        ("a1", "s1", "e1", "fact", "x", "2026-07-07T00:00:00+00:00", 0),
    )
    conn.commit()
    return conn


def test_migration_adds_five_version_columns_to_legacy_table():
    conn = _open_legacy_db()
    migrate_memory_atoms_table(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    assert "extraction_version" in cols
    assert "embedding_model" in cols
    assert "embedding_version" in cols
    assert "extractor_prompt_version" in cols
    assert "source_pipeline_version" in cols


def test_migration_preserves_existing_rows_and_fills_defaults():
    conn = _open_legacy_db()
    migrate_memory_atoms_table(conn)
    row = conn.execute(
        "SELECT atom_id, extraction_version, embedding_model, embedding_version, "
        "extractor_prompt_version, source_pipeline_version FROM memory_atoms WHERE atom_id='a1'"
    ).fetchone()
    assert row == ("a1", "v1", "", 0, "v1", "transcript")


def test_migration_is_idempotent_on_repeat_run():
    conn = _open_legacy_db()
    migrate_memory_atoms_table(conn)
    migrate_memory_atoms_table(conn)  # second call must be a no-op
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    assert len(cols) == 15  # 7 legacy + 5 version + 3 speaker, no duplicates


def test_migration_on_fresh_db_with_full_schema_is_no_op():
    """An already-migrated table should not double-add columns."""
    conn = _open_legacy_db()
    migrate_memory_atoms_table(conn)
    # Simulate the post-migration schema: a fresh table with all 15 columns.
    fresh = sqlite3.connect(":memory:")
    fresh.executescript(
        """
        CREATE TABLE memory_atoms (
            atom_id TEXT PRIMARY KEY, session_id TEXT, source_event_id TEXT, kind TEXT,
            text TEXT, created_at TEXT, start_ms INTEGER,
            extraction_version TEXT NOT NULL DEFAULT 'v1',
            embedding_model TEXT NOT NULL DEFAULT '',
            embedding_version INTEGER NOT NULL DEFAULT 0,
            extractor_prompt_version TEXT NOT NULL DEFAULT 'v1',
            source_pipeline_version TEXT NOT NULL DEFAULT 'transcript',
            speaker TEXT,
            speaker_confidence REAL,
            speaker_assignment TEXT
        );
        """
    )
    migrate_memory_atoms_table(fresh)
    cols = {row[1] for row in fresh.execute("PRAGMA table_info(memory_atoms)")}
    assert len(cols) == 15


def test_migrate_memory_atoms_now_adds_speaker_columns():
    conn = _open_legacy_db()
    migrate_memory_atoms_table(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    assert {"speaker", "speaker_confidence", "speaker_assignment"} <= cols


def _open_legacy_capture_events() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE capture_events (
            event_id TEXT PRIMARY KEY, session_id TEXT, seq INTEGER, kind TEXT,
            created_at TEXT, text TEXT, duration_ms INTEGER, start_ms INTEGER
        );
        """
    )
    return conn


def test_migrate_capture_events_adds_three_speaker_columns():
    conn = _open_legacy_capture_events()
    migrate_capture_events_table(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(capture_events)")}
    assert {"speaker", "speaker_confidence", "speaker_assignment"} <= cols


def test_migrate_capture_events_is_idempotent():
    conn = _open_legacy_capture_events()
    migrate_capture_events_table(conn)
    migrate_capture_events_table(conn)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(capture_events)")}
    assert len(cols) == 11  # 8 + 3


# --- SqliteMemoryIndex occurred_at migration (task-1, D6 production fix) ---


def test_sqlite_memory_index_migrates_old_eight_column_schema(tmp_path):
    """A real pre-task-1 memory_index.db has exactly the eight columns from
    index.py:141-150, no occurred_at column. Opening SqliteMemoryIndex on it
    must add occurred_at without losing or altering any existing row —
    including the vector, byte-for-byte.
    """
    path = tmp_path / "memory_index.db"
    vector_json = json.dumps([0.125, -0.5, 1.0, 0.0])
    conn = sqlite3.connect(str(path))
    conn.executescript(
        """
        CREATE TABLE memory_index (
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
    conn.execute(
        "INSERT INTO memory_index "
        "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, vector) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("a1", "s1", "e1", "fact", "hello world", "2026-07-07T00:00:00+00:00", 0, vector_json),
    )
    conn.commit()
    conn.close()

    SqliteMemoryIndex(path)  # triggers the migration on open

    conn = sqlite3.connect(str(path))
    cols = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)")}
    assert "occurred_at" in cols

    row = conn.execute(
        "SELECT text, created_at, vector, occurred_at FROM memory_index WHERE atom_id = 'a1'"
    ).fetchone()
    conn.close()
    assert row is not None
    text, created_at, vector, occurred_at = row
    assert text == "hello world"
    assert created_at == "2026-07-07T00:00:00+00:00"
    assert vector == vector_json  # byte-identical
    assert occurred_at is None

    # Idempotent: opening a second time must not error.
    SqliteMemoryIndex(path)
    conn = sqlite3.connect(str(path))
    cols_again = {row[1] for row in conn.execute("PRAGMA table_info(memory_index)")}
    conn.close()
    assert cols_again == cols
