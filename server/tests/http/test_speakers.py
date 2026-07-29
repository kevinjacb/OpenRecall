"""Tests for GET /speakers — lists the registry without biometrics."""
from __future__ import annotations

from aiohttp.test_utils import TestClient, TestServer

from sense_server.auth import load_or_create_token
from sense_server.events.store import InMemoryEventStore
from sense_server.http.app import build_app
from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


def _speaker(speaker_id, display_name, is_wearer=False):
    return Speaker(
        speaker_id=speaker_id, display_name=display_name, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=None, embedding_model="fake",
        dim=8, turn_count=3, first_seen="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
    )


async def _client(tmp_path, registry):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(),
        session_lifecycle=SessionLifecycle(),
        event_store=InMemoryEventStore(),
        speaker_registry=registry,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token


async def test_speakers_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, InMemorySpeakerRegistry(SpeakerConfig()))
    try:
        resp = await cli.get("/speakers")
        assert resp.status == 401
    finally:
        await cli.close()


async def test_speakers_empty_when_no_registry(tmp_path):
    cli, token = await _client(tmp_path, None)
    try:
        resp = await cli.get("/speakers", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        assert await resp.json() == {"speakers": []}
    finally:
        await cli.close()


async def test_speakers_empty_registry_returns_empty_list(tmp_path):
    cli, token = await _client(tmp_path, InMemorySpeakerRegistry(SpeakerConfig()))
    try:
        resp = await cli.get("/speakers", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        assert await resp.json() == {"speakers": []}
    finally:
        await cli.close()


async def test_speakers_lists_all_excluding_biometrics(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", "Sarah"))
    reg.add_speaker(_speaker("you", "You", is_wearer=True))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.get("/speakers", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        ids = sorted(s["speakerId"] for s in body["speakers"])
        assert ids == ["sp-1", "you"]
        by_id = {s["speakerId"]: s for s in body["speakers"]}
        assert by_id["sp-1"]["displayName"] == "Sarah"
        assert by_id["sp-1"]["isWearer"] is False
        assert by_id["you"]["isWearer"] is True
        # Biometric guarantee: these keys must NEVER be present over HTTP.
        for s in body["speakers"]:
            assert "centroid" not in s
            assert "embeddingModel" not in s
            assert "embedding_model" not in s
            assert "dim" not in s
            assert "enrollmentStatus" in s
            assert "turnCount" in s
            assert "firstSeen" in s
            assert "updatedAt" in s
    finally:
        await cli.close()