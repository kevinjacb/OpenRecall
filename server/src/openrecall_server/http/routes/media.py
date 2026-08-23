"""POST /media/snapshots — device snapshot upload -> scene atom (P3 §3.3).

The JPEG is the raw body; metadata is query params. Gated by
``capture.vision_enabled`` (403 when off) and by a configured vision pipeline
(503 when none). Maps the snapshot's ``rel_ts_ms`` to a session via the
``SessionTimelineIndex`` + ``SessionIndex`` to derive ``occurred_at``; unmatched
snapshots get ``occurred_at = upload time`` and ``session_id = None``.
"""
from __future__ import annotations

from datetime import datetime, timezone
import os

from aiohttp import web

from openrecall_server.media.blob import sha256_hex, sniff_media_type
from openrecall_server.vision.clip import split_mjpeg, select_keyframes
from openrecall_server.vision.config import VisionConfig


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


async def get_blob(request: web.Request) -> web.Response:
    """Serve a stored blob by digest (P4b — the master spec's deferred-v1.1).

    Content-Type is sniffed from magic bytes (no media_type sidecar). 404 if
    the blob is absent — including blobs already swept by retention (the
    tiered policy: scene atoms are kept forever, image blobs for
    ``retention_days``). Bearer auth is enforced by the global middleware.
    """
    blob_store = request.app.get("sense_blob_store")
    if blob_store is None:
        return _error("unavailable", "blob store not configured", 503)
    digest = request.match_info["digest"]
    try:
        data = blob_store.get(digest)
    except KeyError:
        return _error("not_found", "no blob for digest", 404)
    return web.Response(body=data, content_type=sniff_media_type(data))


async def post_video(request: web.Request) -> web.Response:
    """POST /media/videos — MJPEG clip upload -> scene atom per keyframe (P4b).

    The clip is the raw body; metadata is query params. Gated by
    ``capture.vision_enabled`` (403) and a configured vision pipeline (503).
    The ``video`` extra (Pillow) is required for keyframe selection — 503 when
    absent. Splits the MJPEG, picks scene-change keyframes, and calls
    ``pipe.capture`` per keyframe (idempotent blob+caption+atom, same as a
    snapshot). Session mapping mirrors ``post_snapshot``.
    """
    settings = request.app.get("sense_settings_store")
    if settings is None:
        return _error("unavailable", "settings store not configured", 503)
    if not settings.get().capture.vision_enabled:
        return _error("forbidden", "vision is disabled", 403)
    pipe = request.app.get("sense_vision")
    if pipe is None:
        return _error("unavailable", "vision pipeline not configured", 503)

    body = await request.read()
    if not body:
        return _error("bad_request", "empty clip body", 400)

    # Pillow is lazy-imported inside select_keyframes; 503 if the video extra
    # is not installed.
    try:
        frames = split_mjpeg(body)
    except ValueError:
        return _error("bad_request", "body is not an MJPEG stream", 400)

    cfg = VisionConfig.from_env(os.environ)
    try:
        keyframes = select_keyframes(
            frames, threshold=cfg.keyframe_threshold, min_gap=cfg.keyframe_min_gap,
            cap=cfg.keyframe_cap, thumb_size=cfg.keyframe_thumb_size,
        )
    except ImportError:
        return _error(
            "unavailable",
            "video extra not installed (pip install -e '.[video]')", 503,
        )

    rel_ts_ms = _int_param(request, "rel_ts_ms")
    session_id_param = request.query.get("session_id")
    # The keyframes split from an MJPEG stream are JPEGs — caption them as
    # image/jpeg regardless of the clip's container media_type (the sim sends
    # video/x-mjpeg; a real VLM would malform the request given the container
    # type). The media_type query param is still accepted (aiohttp ignores
    # unused query params) but no longer drives the captioner.
    timeline = request.app.get("sense_session_timeline")
    sidx = request.app.get("sense_session_index")
    clock = request.app.get("sense_clock")
    now = clock.now() if clock is not None else datetime.now(timezone.utc)

    # Resolve session + occurred_at once for the whole clip (rel_ts = clip start).
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
    atoms_out = []
    any_new = False
    for idx in keyframes:
        frame = frames[idx]
        atom = pipe.capture(
            session_id, frame, captured_at_ms=captured_at_ms,
            media_type="image/jpeg", occurred_at=occurred_at,
        )
        digest = sha256_hex(frame)
        atom_id = (f"{session_id}:scene:{digest}" if session_id else f"scene:{digest}")
        atoms_out.append({"atom_id": atom_id, "digest": digest})
        if atom is not None:
            any_new = True
    status = 201 if any_new else 200
    return web.json_response(
        {"schema_version": "v1", "session_id": session_id, "atoms": atoms_out},
        status=status,
    )


def add_routes(app: web.Application) -> None:
    app.router.add_post("/media/snapshots", post_snapshot)
    app.router.add_post("/media/videos", post_video)
    app.router.add_get("/media/blob/{digest}", get_blob)
