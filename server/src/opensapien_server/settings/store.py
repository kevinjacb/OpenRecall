"""Durable settings storage (spec §4.1).

An open key-value table under a typed façade. The table lets a new setting
land without a migration; the façade means every reader gets the same typed
document and nobody parses ``"true"`` by hand.

The store also holds **last-known device state** — what the device was last
observed to be doing, as distinct from what the user asked for. Keeping both
is what makes reconciliation possible at all (spec §4.2): without a
last-known value there is nothing to compare desired state against, and the
server would have to re-issue a command on every reconnect.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol, runtime_checkable

from .model import SettingsDocument

# The one key the settings document lives under. Storage is open, but the
# document is written whole so a partial read can never produce a half-applied
# configuration.
_DOC_KEY = "document"
_DEVICE_STATE_KEY = "device_state"


@runtime_checkable
class SettingsStore(Protocol):
    def get(self) -> SettingsDocument: ...

    def put(self, document: SettingsDocument) -> SettingsDocument: ...

    def get_device_state(self) -> dict: ...

    def put_device_state(self, state: dict) -> None: ...


class InMemorySettingsStore:
    def __init__(self, document: SettingsDocument | None = None) -> None:
        self._doc = document or SettingsDocument()
        self._device: dict = {}
        self._lock = threading.Lock()

    def get(self) -> SettingsDocument:
        with self._lock:
            return self._doc

    def put(self, document: SettingsDocument) -> SettingsDocument:
        with self._lock:
            self._doc = document
            return self._doc

    def get_device_state(self) -> dict:
        with self._lock:
            return dict(self._device)

    def put_device_state(self, state: dict) -> None:
        with self._lock:
            self._device = dict(state)


class SqliteSettingsStore:
    """Durable :class:`SettingsStore`.

    Same shared-connection-plus-lock shape as the other stores: the gateway
    writes device state from a worker thread while HTTP handlers read the
    document on the event loop.
    """

    def __init__(self, path: str | Path) -> None:
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS settings (
                key        TEXT PRIMARY KEY,
                value      TEXT NOT NULL,   -- JSON
                updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def _read(self, key: str) -> dict | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM settings WHERE key = ?", (key,),
            ).fetchone()
        if row is None:
            return None
        try:
            return json.loads(row[0])
        except (json.JSONDecodeError, TypeError):
            return None

    def _write(self, key: str, value: dict) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO settings (key, value, updated_at) "
                "VALUES (?, ?, ?)",
                (key, json.dumps(value), datetime.now(timezone.utc).isoformat()),
            )
            self._conn.commit()

    def get(self) -> SettingsDocument:
        """The stored document, or defaults.

        A row that no longer validates (a field removed in a later version,
        a hand-edited database) falls back to defaults rather than raising:
        the settings screen failing to load is a worse outcome than one
        stale value reverting, and the next ``PUT`` heals it.
        """
        raw = self._read(_DOC_KEY)
        if raw is None:
            return SettingsDocument()
        try:
            return SettingsDocument.model_validate(raw)
        except Exception:
            return SettingsDocument()

    def put(self, document: SettingsDocument) -> SettingsDocument:
        self._write(_DOC_KEY, document.model_dump(mode="json"))
        return document

    def get_device_state(self) -> dict:
        return self._read(_DEVICE_STATE_KEY) or {}

    def put_device_state(self, state: dict) -> None:
        self._write(_DEVICE_STATE_KEY, state)
