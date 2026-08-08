"""Tests for the AuditLogger (M5.2)."""
from __future__ import annotations

import json
import sqlite3
import tempfile
from pathlib import Path

import pytest

from openrecall_server.agent.audit import InMemoryAuditLogger, SqliteAuditLogger


def test_in_memory_audit_logger_records_entries():
    a = InMemoryAuditLogger()
    aid1 = a.record({"request_id": "r1", "outcome": "return"})
    aid2 = a.record({"request_id": "r2", "outcome": "refuse"})
    assert aid1 != aid2
    assert len(a.entries) == 2


def test_sqlite_audit_logger_records_and_retrieves_by_request_id():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.db"
        a = SqliteAuditLogger(path)
        a.record({"request_id": "r1", "retrieval_trace_id": "t1", "outcome": "return", "raw": "...", "prompt_hash": "h1"})
        a.record({"request_id": "r2", "outcome": "refuse", "raw": "...", "prompt_hash": "h2"})
        all_entries = a.entries()
        assert len(all_entries) == 2
        r1 = a.entries(request_id="r1")
        assert len(r1) == 1
        assert r1[0]["retrieval_trace_id"] == "t1"
        assert r1[0]["outcome"] == "return"


def test_sqlite_audit_logger_creates_indexes():
    """M5.2: the three indexes (request_id, retrieval_trace_id, ts) must exist."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.db"
        SqliteAuditLogger(path)
        with sqlite3.connect(str(path)) as conn:
            rows = conn.execute("PRAGMA index_list(audit_entries)").fetchall()
        names = {r[1] for r in rows}
        assert "ix_audit_request" in names
        assert "ix_audit_trace" in names
        assert "ix_audit_ts" in names


def test_sqlite_audit_logger_idempotent_on_schema_init():
    """Constructing twice on the same file does not error."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "audit.db"
        SqliteAuditLogger(path)
        SqliteAuditLogger(path)  # must not raise
