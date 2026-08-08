"""Track currently-open sessions (one per ``GatewayCore`` in the WS handler).

A :class:`SessionLifecycle` is a tiny thread-safe set of ``session_id``\ s
for sessions that have an active WebSocket connection. It is the source
of truth for the ``activeSessions`` field in the ``/status`` response —
that field is a connection count, not a session count, so it lives
separately from the durable :class:`~openrecall_server.sessions.index.SessionIndex`.

``register`` is called from ``GatewayCore._on_hello``; ``deregister`` is
called from ``GatewayCore._close_session``, which runs both on ``bye``
and — the load-bearing path — from ``GatewayCore.on_disconnect`` in the
transport's ``finally`` block. ``bye`` is unreliable (a WebSocket drop
writes it into a dead socket), so without the disconnect path an abrupt
disconnect leaks the session id here forever. The set is
process-wide, in-memory; multi-process deployments would need a
shared registry (Redis, ZooKeeper) — YAGNI for v1.
"""
from __future__ import annotations

import threading


class SessionLifecycle:
    """Thread-safe registry of currently-open session ids.

    The HTTP layer calls :meth:`__len__` for the ``activeSessions`` count.
    Tests call :meth:`register` / :meth:`deregister` directly to drive
    state changes.
    """

    def __init__(self) -> None:
        self._active: set[str] = set()
        self._lock = threading.Lock()

    def register(self, session_id: str) -> None:
        """Mark a session as having an open connection.

        Idempotent: registering a session that's already active is a no-op.
        """
        with self._lock:
            self._active.add(session_id)

    def deregister(self, session_id: str) -> None:
        """Mark a session as no longer having an open connection.

        Idempotent: deregistering an unknown session is a no-op.
        """
        with self._lock:
            self._active.discard(session_id)

    def is_active(self, session_id: str) -> bool:
        with self._lock:
            return session_id in self._active

    def __len__(self) -> int:
        with self._lock:
            return len(self._active)
