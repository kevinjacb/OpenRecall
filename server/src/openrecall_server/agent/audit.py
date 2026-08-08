"""Durable audit log of every decision on the cognitive read path (M5.2 / H5).

The audit log is the binding artefact for end-to-end traceability: every
Planner run writes one row capturing trigger, retrieved atom ids, raw
LLM output, validated action, guardrail decision, command_id (if any),
and lifecycle transitions. The indexes on ``request_id`` and
``retrieval_trace_id`` make operator queries O(log n); the index on
``ts`` supports time-bounded scans.

The audit write is best-effort: a failure here must never lose the
Planners response (H3). The Planner catches the exception and logs it.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class AuditLogger(Protocol):
    """Single seam for "write one audit entry"."""

    def record(self, entry: dict) -> str:
        """Append one entry; return the generated ``audit_id``."""
        ...


class InMemoryAuditLogger:
    """Process-local audit logger for tests."""

    def __init__(self) -> None:
        self.entries: list[dict] = []
        self._counter = 0

    def record(self, entry: dict) -> str:
        self._counter += 1
        entry = dict(entry)
        entry.setdefault("audit_id", f"audit-{self._counter:06d}")
        self.entries.append(entry)
        return entry["audit_id"]


class SqliteAuditLogger:
    """Durable audit logger — one row per call, indexed for queries.

    Schema (idempotent, additive):

      CREATE TABLE audit_entries (
        audit_id          TEXT PRIMARY KEY,
        request_id        TEXT NOT NULL,
        retrieval_trace_id TEXT,
        ts                TEXT NOT NULL,
        outcome           TEXT NOT NULL,
        raw               TEXT,
        prompt_hash       TEXT,
        validated         TEXT,
        guarded           TEXT,
        latency_ms        INTEGER,
        ... user-defined fields stored as JSON in `payload` ...
      );
    """

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS audit_entries (
                audit_id           TEXT PRIMARY KEY,
                request_id         TEXT NOT NULL,
                retrieval_trace_id TEXT,
                ts                 TEXT NOT NULL,
                outcome            TEXT NOT NULL,
                raw                TEXT,
                prompt_hash        TEXT,
                validated          TEXT,
                guarded            TEXT,
                latency_ms         INTEGER,
                payload            TEXT
            );
            CREATE INDEX IF NOT EXISTS ix_audit_request
                ON audit_entries (request_id);
            CREATE INDEX IF NOT EXISTS ix_audit_trace
                ON audit_entries (retrieval_trace_id);
            CREATE INDEX IF NOT EXISTS ix_audit_ts
                ON audit_entries (ts);
            """
        )
        self._conn.commit()
        self._counter = 0

    def record(self, entry: dict) -> str:
        self._counter += 1
        audit_id = entry.get("audit_id") or f"audit-{self._counter:08d}"
        payload = {k: v for k, v in entry.items() if k not in {
            "audit_id", "request_id", "retrieval_trace_id", "ts", "outcome",
            "raw", "prompt_hash", "validated", "guarded", "latency_ms",
        }}
        ts = entry.get("ts") or datetime.now(__import__("datetime").timezone.utc).isoformat()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO audit_entries "
                "(audit_id, request_id, retrieval_trace_id, ts, outcome, "
                " raw, prompt_hash, validated, guarded, latency_ms, payload) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    audit_id,
                    entry.get("request_id", ""),
                    entry.get("retrieval_trace_id"),
                    ts,
                    entry.get("outcome", ""),
                    entry.get("raw"),
                    entry.get("prompt_hash"),
                    json.dumps(entry.get("validated")) if entry.get("validated") is not None else None,
                    json.dumps(entry.get("guarded")) if entry.get("guarded") is not None else None,
                    entry.get("latency_ms"),
                    json.dumps(payload, default=str) if payload else None,
                ),
            )
            self._conn.commit()
        return audit_id

    def entries(self, request_id: str | None = None) -> list[dict]:
        """Return audit entries, optionally filtered by request_id."""
        with self._lock:
            if request_id is None:
                rows = self._conn.execute(
                    "SELECT audit_id, request_id, retrieval_trace_id, ts, outcome, "
                    "raw, prompt_hash, validated, guarded, latency_ms, payload "
                    "FROM audit_entries ORDER BY ts"
                ).fetchall()
            else:
                rows = self._conn.execute(
                    "SELECT audit_id, request_id, retrieval_trace_id, ts, outcome, "
                    "raw, prompt_hash, validated, guarded, latency_ms, payload "
                    "FROM audit_entries WHERE request_id = ? ORDER BY ts",
                    (request_id,),
                ).fetchall()
        return [
            {
                "audit_id": r[0],
                "request_id": r[1],
                "retrieval_trace_id": r[2],
                "ts": r[3],
                "outcome": r[4],
                "raw": r[5],
                "prompt_hash": r[6],
                "validated": json.loads(r[7]) if r[7] else None,
                "guarded": json.loads(r[8]) if r[8] else None,
                "latency_ms": r[9],
                "payload": json.loads(r[10]) if r[10] else None,
            }
            for r in rows
        ]
