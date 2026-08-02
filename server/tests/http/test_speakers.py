"""Tests for GET /speakers — lists the registry without biometrics."""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from sense_server.auth import load_or_create_token
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.http.app import build_app
from sense_server.ingest.speaker_config import SpeakerConfig
from sense_server.memory.atom import MemoryAtom
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker
from sense_server.memory.store import InMemoryAtomStore
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


def _speaker(speaker_id, display_name, is_wearer=False):
    return Speaker(
        speaker_id=speaker_id, display_name=display_name, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=None, embedding_model="fake",
        dim=8, turn_count=3, first_seen="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
    )


async def _client(tmp_path, registry, atom_store=None):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(),
        session_lifecycle=SessionLifecycle(),
        event_store=InMemoryEventStore(),
        atom_store=atom_store,
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


async def test_rename_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, InMemorySpeakerRegistry(SpeakerConfig()))
    try:
        resp = await cli.post("/speakers/sp-1/rename", json={"name": "Sarah"})
        assert resp.status == 401
    finally:
        await cli.close()


async def test_rename_409_when_disabled(tmp_path):
    cli, token = await _client(tmp_path, None)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 409
        assert (await resp.json())["error"] == "speaker_recognition_disabled"
    finally:
        await cli.close()


async def test_rename_400_on_empty_name(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", "Sarah"))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


async def test_rename_404_unknown_speaker(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/nope/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 404
    finally:
        await cli.close()


async def test_rename_returns_updated_speaker_without_biometrics(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", None))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 200
        body = await resp.json()
        sp = body["speaker"]
        assert sp["speakerId"] == "sp-1"
        assert sp["displayName"] == "Sarah"
        assert sp["enrollmentStatus"] == "confirmed"
        for key in ("centroid", "embeddingModel", "embedding_model", "dim"):
            assert key not in sp
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


async def test_reassign_requires_token(tmp_path):
    cli, _ = await _client(
        tmp_path, InMemorySpeakerRegistry(SpeakerConfig()), InMemoryAtomStore()
    )
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "all"},
        )
        assert resp.status == 401
    finally:
        await cli.close()


async def test_reassign_409_when_disabled(tmp_path):
    cli, token = await _client(tmp_path, None, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 409
    finally:
        await cli.close()


async def test_reassign_400_on_bad_scope(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    cli, token = await _client(tmp_path, reg, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "one"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


async def test_reassign_404_unknown_speaker(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("a", "A"))
    cli, token = await _client(tmp_path, reg, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "missing"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 404
    finally:
        await cli.close()


async def test_reassign_400_on_self_reassign(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("a", "A"))
    cli, token = await _client(tmp_path, reg, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "a", "scope": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
        body = await resp.json()
        assert body["error"] == "bad_request"
    finally:
        await cli.close()


async def test_reassign_204_rel_labels_events_and_atoms(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("a", "A"))
    reg.add_speaker(_speaker("b", "B", is_wearer=True))
    atoms = InMemoryAtomStore()
    events = InMemoryEventStore()
    # NOTE: build_app wires event_store=InMemoryEventStore() internally; to
    # assert relabeling we must use the SAME store the app uses. Re-build the
    # app by hand so the stores are the instances we seed:
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token, get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(), session_lifecycle=SessionLifecycle(),
        event_store=events, atom_store=atoms, speaker_registry=reg,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        for seq in range(2):
            events.append(CaptureEvent(
                event_id=f"s:{seq}", session_id="s", seq=seq, kind="transcript",
                created_at=datetime(2026, 8, 1, tzinfo=timezone.utc), text="x",
                duration_ms=1000, start_ms=seq * 1000, speaker="a",
                speaker_confidence=0.9, speaker_assignment="confirmed"))
        atoms.append(MemoryAtom(
            atom_id="m1", session_id="s", source_event_id="s:0", kind="fact",
            text="y", created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            start_ms=0, speaker="a", speaker_confidence=0.9,
            speaker_assignment="confirmed"))

        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 204

        assert [e.speaker for e in events.events("s")] == ["b", "b"]
        assert atoms.atoms("s")[0].speaker == "b"
    finally:
        await cli.close()