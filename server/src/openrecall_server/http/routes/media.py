"""POST /media/snapshots — device snapshot upload -> scene atom (P3 §3.3).

The JPEG is the raw body; metadata is query params. Gated by
``capture.vision_enabled`` (403 when off) and by a configured vision pipeline
(503 when none). Maps the snapshot's ``rel_ts_ms`` to a session via the
``SessionTimelineIndex`` + ``SessionIndex`` to derive ``occurred_at``; unmatched
snapshots get ``occurred_at = upload time`` and ``session_id = None``.
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp import web

from openrecall_server.media.blob import sha256_hex


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response(
        {"schema_version": "v1", "code": code, "message": message}, status=status,
    )


def _int_param(request: web.Request, name: str) -> int | None:
    raw = request.query.get(name)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


async def post_snapshot(request: web.Request) -> web.Response:
    settings = request.app.get("sense_settings_store")
    if settings is None:
        return _error("unavailable", "settings store not configured", 503)
    if not settings.get().capture.vision_enabled:
        return _error("forbidden", "vision is disabled", 403)

    pipe = request.app.get("sense_vision")
    if pipe is None:
        return _error("unavailable", "vision pipeline not configured", 503)

    image = await request.read()
    if not image:
        return _error("bad_request", "empty image body", 400)

    rel_ts_ms = _int_param(request, "rel_ts_ms")
    session_id_param = request.query.get("session_id")
    media_type = request.query.get("media_type", "image/jpeg")
    # boot_id accepted for forward-compat, unused in v1 (single-boot).

    timeline = request.app.get("sense_session_timeline")
    sidx = request.app.get("sense_session_index")
    clock = request.app.get("sense_clock")
    now = clock.now() if clock is not None else datetime.now(timezone.utc)

    session_id = session_id_param
    occurred_at = now
    if session_id is None and timeline is not None and rel_ts_ms is not None:
        session_id = timeline.session_for_rel_ts(rel_ts_ms)
    if session_id is not None and sidx is not None and timeline is not None and rel_ts_ms is not None:
        rng = timeline.rel_range(session_id)
        summary = sidx.summary(session_id)
        if rng is not None and summary is not None:
            occurred_at = datetime.fromtimestamp(
                summary.started_at.timestamp() + (rel_ts_ms - rng[0]) / 1000.0,
                tz=timezone.utc,
            )

    captured_at_ms = rel_ts_ms if rel_ts_ms is not None else 0
    atom = pipe.capture(
        session_id, image, captured_at_ms=captured_at_ms,
        media_type=media_type, occurred_at=occurred_at,
    )
    digest = sha256_hex(image)
    atom_id = f"{session_id}:scene:{digest}" if session_id else f"scene:{digest}"
    status = 200 if atom is None else 201   # None = idempotent replay
    return web.json_response(
        {"schema_version": "v1", "atom_id": atom_id,
         "session_id": session_id, "digest": digest},
        status=status,
    )


def add_routes(app: web.Application) -> None:
    app.router.add_post("/media/snapshots", post_snapshot)