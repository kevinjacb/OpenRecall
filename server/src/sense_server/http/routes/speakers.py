"""GET /speakers — list the speaker registry without biometrics.

The biometric fields (``centroid``, ``embedding_model``, ``dim``) are
NEVER serialized here. Speaker embeddings never leave the Mac unless
``SENSE_SPEAKER_EMBED_BASE_URL`` is explicitly set (unchanged).
"""
from __future__ import annotations

from aiohttp import web


def add_routes(app: web.Application) -> None:
    app.router.add_get("/speakers", list_speakers)


def _registry(app: web.Application):
    return app.get("sense_speaker_registry")


def _speaker_to_wire(s) -> dict:
    """Serialize a Speaker WITHOUT biometrics (centroid/embedding_model/dim)."""
    return {
        "speakerId": s.speaker_id,
        "displayName": s.display_name,
        "isWearer": s.is_wearer,
        "enrollmentStatus": s.enrollment_status,
        "turnCount": s.turn_count,
        "firstSeen": s.first_seen,
        "updatedAt": s.updated_at,
    }


async def list_speakers(request: web.Request) -> web.Response:
    """List all known speakers, minus biometrics. Empty list when disabled."""
    reg = _registry(request.app)
    if reg is None:
        return web.json_response({"speakers": []})
    try:
        speakers = reg.list_speakers()
    except Exception:  # pragma: no cover - last-resort safety net
        return web.json_response({"speakers": []})
    return web.json_response({"speakers": [_speaker_to_wire(s) for s in speakers]})