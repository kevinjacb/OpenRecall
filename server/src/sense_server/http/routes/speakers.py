"""GET /speakers — list the speaker registry without biometrics.

The biometric fields (``centroid``, ``embedding_model``, ``dim``) are
NEVER serialized here. Speaker embeddings never leave the Mac unless
``SENSE_SPEAKER_EMBED_BASE_URL`` is explicitly set (unchanged).
"""
from __future__ import annotations

from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field


def add_routes(app: web.Application) -> None:
    app.router.add_get("/speakers", list_speakers)
    app.router.add_post("/speakers/reassign", reassign_speaker_route)
    app.router.add_post("/speakers/{speaker_id}/rename", rename_speaker)


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


class RenameSpeakerDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class ReassignSpeakerDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fromSpeakerId: str = Field(min_length=1)
    toSpeakerId: str = Field(min_length=1)
    scope: Literal["all"] = "all"


def _bad_request(message: str) -> web.Response:
    return web.json_response({"error": "bad_request", "message": message}, status=400)


async def rename_speaker(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    if reg is None:
        return web.json_response({"error": "speaker_recognition_disabled"}, status=409)
    speaker_id = request.match_info["speaker_id"]
    try:
        body = await request.json()
        dto = RenameSpeakerDTO.model_validate(body)
    except Exception as e:
        return _bad_request(f"invalid request: {e}")
    try:
        reg.name(speaker_id, dto.name)
    except KeyError:
        return web.json_response({"error": "speaker_not_found"}, status=404)
    return web.json_response({"speaker": _speaker_to_wire(reg.get(speaker_id))})


async def reassign_speaker_route(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    store = request.app.get("sense_event_store")
    atoms = request.app.get("sense_atom_store")
    if reg is None or store is None or atoms is None:
        return web.json_response({"error": "speaker_recognition_disabled"}, status=409)
    try:
        body = await request.json()
        dto = ReassignSpeakerDTO.model_validate(body)
    except Exception as e:
        return _bad_request(f"invalid request: {e}")
    if reg.get(dto.fromSpeakerId) is None or reg.get(dto.toSpeakerId) is None:
        return web.json_response({"error": "speaker_not_found"}, status=404)
    if dto.fromSpeakerId == dto.toSpeakerId:
        return _bad_request("fromSpeakerId and toSpeakerId must differ")
    from sense_server.memory.speaker_registry import reassign_speaker

    reassign_speaker(reg, store, atoms, dto.fromSpeakerId, dto.toSpeakerId, dto.scope)
    return web.Response(status=204)