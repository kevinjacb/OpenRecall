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
    gateway_port: int | None = None,
    planner=None,
    retriever=None,
    atom_store=None,
    metrics=None,
    id_generator=None,
):
    """Build the Sense HTTP control API.

    Optional dependencies (``event_store``, ``session_index``,
    ``session_lifecycle``) are stashed on ``app[...]`` for the route
    handlers to read. The boot time is captured as ``app["sense_started_at"]``
    in monotonic seconds (paired with :func:`time.monotonic`) for the
    ``/status`` uptime counter.

    ``gateway_port`` is the port the WS gateway listens on — a separate
    server from this HTTP API. ``/health`` advertises it so the phone can
    derive its WebSocket URL: the HTTP API and the WS gateway run on
    different ports, so the phone can't reach the gateway by swapping only
    the scheme on the HTTP URL (it would hit the HTTP port, which has no WS
    route, and fail the ``101`` upgrade). ``None`` (the default) omits the
    field for back-compat with callers/tests that don't run the gateway.
    """
    app = web.Application(middlewares=[bearer_auth_middleware])
    app["sense_token"] = token
    app["sense_get_pubkey"] = get_pubkey
    app["sense_event_store"] = event_store
    app["sense_session_index"] = session_index
    app["sense_session_lifecycle"] = session_lifecycle
    app["sense_gateway_port"] = gateway_port
    app["sense_started_at"] = time.monotonic()
    app["sense_planner"] = planner
    app["sense_retriever"] = retriever
    app["sense_atom_store"] = atom_store
    app["sense_metrics"] = metrics
    app["sense_id_generator"] = id_generator

    from sense_server.http.routes.provisioning import add_routes as add_provisioning
    from sense_server.http.routes.sessions import add_routes as add_sessions
    from sense_server.http.routes.status import add_routes as add_status
    from sense_server.http.routes.agent import add_routes as add_agent
    from sense_server.http.routes.memory import add_routes as add_memory
    from sense_server.http.routes.metrics_route import add_routes as add_metrics_route

    add_provisioning(app)
    add_sessions(app)
    add_status(app)
    add_agent(app)
    add_memory(app)
    add_metrics_route(app)
    return app
