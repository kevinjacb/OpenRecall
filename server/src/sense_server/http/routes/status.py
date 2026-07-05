"""HTTP /status route — a single endpoint reporting server health.

Returns the server's view of its own state: reachability (always true if
the request reached the route), authenticated (the middleware already
validated the token), package version, uptime since process boot, the
count of open WS connections, the count of distinct sessions ever seen,
and the count of events seen in the last 24h.

Wire shape matches the Android ``ServerStatusDto`` (camelCase keys).
"""
from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ...sessions.index import SessionIndex
    from ...sessions.lifecycle import SessionLifecycle


def add_routes(app: web.Application) -> None:
    app.router.add_get("/status", get_status)


def _version() -> str:
    """The package version, via `__version__` (set in src/sense_server/__init__.py).

    Falls back to ``importlib.metadata`` if the attribute is removed in a
    future refactor, then to the literal string ``"unknown"`` if neither
    is available.
    """
    from sense_server import __version__ as v
    if isinstance(v, str) and v:
        return v
    try:
        from importlib.metadata import version
        return version("sense-server")
    except Exception:  # pragma: no cover - last-resort fallback
        return "unknown"


def _safe(detail: str) -> web.Response:
    """Return a 500-ish payload with a precise error code (never crashes the UI)."""
    return web.json_response(
        {
            "reachable": True,
            "authenticated": True,
            "version": _version(),
            "uptimeSeconds": 0,
            "activeSessions": 0,
            "totalSessions": 0,
            "recentEvents24h": 0,
            "error": detail,
        },
        status=200,  # /status never errors — partial data is better than no data
    )


async def get_status(request: web.Request) -> web.Response:
    app = request.app
    index: "SessionIndex | None" = app.get("sense_session_index")
    lifecycle: "SessionLifecycle | None" = app.get("sense_session_lifecycle")
    started_at: float = app.get("sense_started_at", time.monotonic())

    try:
        return web.json_response(
            {
                "reachable": True,
                "authenticated": True,
                "version": _version(),
                "uptimeSeconds": max(0, int(time.monotonic() - started_at)),
                "activeSessions": len(lifecycle) if lifecycle is not None else 0,
                "totalSessions": index.total_sessions() if index is not None else 0,
                "recentEvents24h": index.recent_events_24h() if index is not None else 0,
            }
        )
    except Exception as e:  # pragma: no cover - last-resort safety net
        return _safe(repr(e))
