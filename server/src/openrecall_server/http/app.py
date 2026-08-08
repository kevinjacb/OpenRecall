"""The OpenRecall HTTP control API factory.

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

from openrecall_server.contracts.clock import SystemClock
from openrecall_server.events.store import EventStore
from openrecall_server.http.auth import bearer_auth_middleware
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.lifecycle import SessionLifecycle


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
    command_store=None,
    command_dispatcher=None,
    speaker_registry=None,
    segment_index=None,
    segment_meta=None,
    audio_store=None,
    settings_store=None,
    liveness=None,
    capability_provider=None,
    clock=None,
    reconciler=None,
    memory_index=None,
):
    """Build the OpenRecall HTTP control API.

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
    app["sense_command_store"] = command_store
    app["sense_command_dispatcher"] = command_dispatcher
    app["sense_speaker_registry"] = speaker_registry
    app["sense_segment_index"] = segment_index
    app["sense_segment_meta"] = segment_meta
    app["sense_audio_store"] = audio_store
    app["sense_settings_store"] = settings_store
    app["sense_liveness"] = liveness
    app["sense_capability_provider"] = capability_provider
    # Defaulted rather than left None: POST /commands needs a clock to stamp
    # issued_at/expires_at, and a wall clock has no configuration worth
    # forcing every caller to supply. Tests inject a FakeClock.
    app["sense_clock"] = clock if clock is not None else SystemClock()
    app["sense_reconciler"] = reconciler
    app["sense_memory_index"] = memory_index

    from openrecall_server.http.routes.provisioning import add_routes as add_provisioning
    from openrecall_server.http.routes.sessions import add_routes as add_sessions
    from openrecall_server.http.routes.status import add_routes as add_status
    from openrecall_server.http.routes.agent import add_routes as add_agent
    from openrecall_server.http.routes.memory import add_routes as add_memory
    from openrecall_server.http.routes.metrics_route import add_routes as add_metrics_route
    from openrecall_server.http.routes.commands import add_routes as add_commands
    from openrecall_server.http.routes.speakers import add_routes as add_speakers
    from openrecall_server.http.routes.segments import add_routes as add_segments
    from openrecall_server.http.routes.settings import add_routes as add_settings
    from openrecall_server.http.routes.device import add_routes as add_device

    add_provisioning(app)
    add_sessions(app)
    add_segments(app)
    add_status(app)
    add_agent(app)
    add_memory(app)
    add_metrics_route(app)
    add_speakers(app)
    add_settings(app)
    add_device(app)
    if command_store is not None and command_dispatcher is not None:
        add_commands(app)
    return app
