"""The Sense HTTP control API factory.

Wires the bearer-token middleware, the per-app shared dependencies (the
event store, session index, session lifecycle, process start time), and
the route modules under a single ``web.Application``.

The factory accepts every dependency as an optional kwarg with a default
of ``None`` for back-compat with the small handful of tests that build
the bare ``build_app(token=..., get_pubkey=...)`` — those tests only
exercise the provisioning routes, which don't need the new dependencies.
The end-to-end ``scripts/run_gateway.py`` is the canonical caller and
wires everything in.
"""
from __future__ import annotations

import time

from aiohttp import web

from sense_server.events.store import EventStore
from sense_server.http.auth import bearer_auth_middleware
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


def build_app(
    *,
    token,
    get_pubkey,
    event_store: EventStore | None = None,
    session_index: SessionIndex | None = None,
    session_lifecycle: SessionLifecycle | None = None,
):
    """Build the Sense HTTP control API.

    Optional dependencies (``event_store``, ``session_index``,
    ``session_lifecycle``) are stashed on ``app[...]`` for the route
    handlers to read. The boot time is captured as ``app["sense_started_at"]``
    in monotonic seconds (paired with :func:`time.monotonic`) for the
    ``/status`` uptime counter.
    """
    app = web.Application(middlewares=[bearer_auth_middleware])
    app["sense_token"] = token
    app["sense_get_pubkey"] = get_pubkey
    app["sense_event_store"] = event_store
    app["sense_session_index"] = session_index
    app["sense_session_lifecycle"] = session_lifecycle
    app["sense_started_at"] = time.monotonic()

    from sense_server.http.routes.provisioning import add_routes as add_provisioning
    from sense_server.http.routes.sessions import add_routes as add_sessions
    from sense_server.http.routes.status import add_routes as add_status

    add_provisioning(app)
    add_sessions(app)
    add_status(app)
    return app
