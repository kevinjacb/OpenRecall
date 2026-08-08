"""HTTP /sessions routes — list, detail, and per-session events.

Three endpoints, all behind the existing ``bearer_auth_middleware``:

* ``GET /sessions`` — page of summaries, ``?limit=N&cursor=…``.
* ``GET /sessions/{id}`` — summary + events in one payload.
* ``GET /sessions/{id}/events`` — the events list alone.

The cursor format is opaque to the client: base64-encoded JSON
``{"before": iso, "last_id": sid}``. The server encodes on the way out
and decodes on the way in; a malformed cursor returns 400 (not a
silent first-page fallback — that hides bugs).

The summary wire shape (camelCase keys) matches the Android
``SessionSummaryDto``; the event wire shape matches ``CaptureEventDto``.
A mapper would be the cleaner long-term boundary, but with a single
server + single Android client pair, the duplication is small and the
test surface is honest.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

from aiohttp import web

from ...sessions.index import SessionSummary

if TYPE_CHECKING:
    from ...events.store import EventStore
    from ...sessions.index import SessionIndex


_LIMIT_DEFAULT = 20
_LIMIT_MAX = 100


def add_routes(app: web.Application) -> None:
    app.router.add_get("/sessions", list_sessions)
    app.router.add_get("/sessions/{session_id}", get_session)
    app.router.add_get("/sessions/{session_id}/events", get_session_events)


# ---- helpers ----------------------------------------------------------------


def _index(app: web.Application) -> "SessionIndex":
    idx = app["sense_session_index"]
    if idx is None:
        raise web.HTTPInternalServerError(
            text=json.dumps({"error": "no_index"}),
            content_type="application/json",
        )
    return idx


def _store(app: web.Application) -> "EventStore":
    store = app["sense_event_store"]
    if store is None:
        raise web.HTTPInternalServerError(
            text=json.dumps({"error": "no_store"}),
            content_type="application/json",
        )
    return store


def _speaker_registry(app: web.Application):
    """The SpeakerRegistry wired into build_app, or None when disabled."""
    return app.get("sense_speaker_registry")


def _audio_store(app: web.Application):
    """The AudioStore wired into build_app, or None when audio is off."""
    return app.get("sense_audio_store")


def _summary_to_wire(s: SessionSummary, *, now: datetime | None = None) -> dict:
    """Map a SessionSummary to the wire shape matching the Android DTO."""
    return {
        "id": s.id,
        "startedAt": s.started_at.isoformat(),
        "endedAt": s.ended_at.isoformat() if s.ended_at is not None else None,
        "durationMs": s.duration_ms(now=now),
        "transcriptCount": s.transcript_count,
        "preview": s.preview or "",
    }


def _event_to_wire(e, registry=None, audio=None) -> dict:
    """Map a CaptureEvent to the wire shape matching the Android DTO.

    Speaker display fields (``speakerName``/``isWearer``) are resolved at
    read time from the registry so renames/reassigns reflect immediately
    without a backfill. The stored event row keeps only the stable
    ``speaker_id`` UUID. ``None``/``False`` when the hop has no speaker or
    the registry has no row (e.g. speaker recognition disabled).

    ``audio`` is the :class:`AudioStore`, used only to fill the codec fields
    honestly: they were hardcoded to ``""``/``0`` while the audio plane did
    not exist. Now that it does, a client can tell "no audio for this
    session" from "audio in an unknown format" — before, both looked the
    same.
    """
    sp = registry.get(e.speaker) if (e.speaker and registry is not None) else None
    has_audio = audio is not None and audio.has(e.session_id)
    return {
        "id": e.event_id,
        "sessionId": e.session_id,
        "seq": e.seq,
        "startMs": e.start_ms,
        "createdAt": e.created_at.isoformat(),
        "kind": e.kind,
        "text": e.text or "",
        "durationMs": e.duration_ms,
        # Populated once the session has a frame log (spec §3.2). Empty/zero
        # means "this session has no retrievable audio", which is now a real
        # distinction rather than a placeholder.
        "codec": "opus" if has_audio else "",
        "sampleRateHz": 16000 if has_audio else 0,
        "byteCount": (
            audio.stat(e.session_id).byte_count if has_audio else 0
        ),
        "speaker": e.speaker,
        "speakerName": sp.display_name if sp is not None else None,
        "isWearer": sp.is_wearer if sp is not None else False,
        "speakerConfidence": e.speaker_confidence,
        "speakerAssignment": e.speaker_assignment,
    }


def _clamp_limit(raw: str | None) -> int:
    if raw is None or raw == "":
        return _LIMIT_DEFAULT
    try:
        n = int(raw)
    except ValueError:
        return _LIMIT_DEFAULT
    if n <= 0:
        return 1
    if n > _LIMIT_MAX:
        return _LIMIT_MAX
    return n


def _bad_cursor() -> web.Response:
    return web.json_response({"error": "bad_cursor"}, status=400)


# ---- endpoints --------------------------------------------------------------


async def list_sessions(request: web.Request) -> web.Response:
    idx = _index(request.app)
    limit = _clamp_limit(request.query.get("limit"))
    cursor = request.query.get("cursor")
    try:
        summaries, next_cursor = idx.list(limit=limit, before=cursor)
    except ValueError:
        return _bad_cursor()
    return web.json_response(
        {
            "sessions": [_summary_to_wire(s) for s in summaries],
            "nextCursor": next_cursor,
        }
    )


async def get_session(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    idx = _index(request.app)
    summary = idx.summary(session_id)
    if summary is None:
        return web.json_response({"error": "not_found"}, status=404)
    events = _store(request.app).events(session_id)
    registry = _speaker_registry(request.app)
    audio = _audio_store(request.app)
    return web.json_response(
        {
            "summary": _summary_to_wire(summary),
            "events": [_event_to_wire(e, registry, audio) for e in events],
        }
    )


async def get_session_events(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    idx = _index(request.app)
    summary = idx.summary(session_id)
    if summary is None:
        return web.json_response({"error": "not_found"}, status=404)
    events = _store(request.app).events(session_id)
    registry = _speaker_registry(request.app)
    audio = _audio_store(request.app)
    return web.json_response({"events": [_event_to_wire(e, registry, audio) for e in events]})
