"""POST /media/snapshots — device snapshot upload -> scene atom (P3 §3.3).

Raw-body JPEG + query-param metadata. 403 when ``vision_enabled`` is off, 503
when no vision pipeline is configured, 201 (new) / 200 (idempotent replay).
"""
from __future__ import annotations

from datetime import datetime, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.contracts.clock import FakeClock
from openrecall_server.events.model import CaptureEvent
from openrecall_server.http.app import build_app
from openrecall_server.media.blob import InMemoryBlobStore, sha256_hex
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.timeline import SessionTimelineIndex
from openrecall_server.settings.model import SettingsDocument
from openrecall_server.settings.store import InMemorySettingsStore
from openrecall_server.vision.pipeline import VisionPipeline

_AUTH = {"Authorization": "Bearer t"}
FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeVision:
    def __init__(self, mapping: dict[bytes, str]) -> None:
        self.mapping = mapping
        self.calls = 0

    def caption(self, image: bytes, *, media_type: str = "image/jpeg") -> str:
        self.calls += 1
        return self.mapping[image]


def _vision_store(image_bytes: bytes, caption: str = "a scene"):
    blobs = InMemoryBlobStore()
    vision = FakeVision({image_bytes: caption})
    atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, vision, atoms, clock=lambda: FIXED)
    return blobs, vision, atoms, pipe


def _client(
    *,
    pipe=None,
    vision_enabled: bool = True,
    timeline=None,
    sidx=None,
    clock=None,
):
    doc = SettingsDocument.model_validate(
        {"capture": {"vision_enabled": vision_enabled}},
    )
    store = InMemorySettingsStore(doc)
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        settings_store=store,
        vision=pipe,
        session_timeline=timeline,
        session_index=sidx,
        clock=clock,
    )
    return TestClient(TestServer(app)), store


def _seed_session(sidx: SessionIndex, sid: str, started_at: datetime) -> None:
    sidx.record(
        CaptureEvent(
            event_id=f"{sid}-e1",
            session_id=sid,
            seq=0,
            kind="transcript",
            created_at=started_at,
            text="hello",
            duration_ms=100,
            start_ms=0,
        ),
    )


async def test_post_snapshot_creates_scene_atom():
    tl = SessionTimelineIndex()
    tl.record("s1", 1000)
    tl.record("s1", 5000)
    sidx = SessionIndex()
    _seed_session(sidx, "s1", FIXED)
    clock = FakeClock(FIXED)
    _blobs, _vision, atoms, pipe = _vision_store(b"img1")
    client, _store = _client(
        pipe=pipe, timeline=tl, sidx=sidx, clock=clock,
    )
    async with client:
        resp = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
            headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 201
    digest = sha256_hex(b"img1")
    assert body == {
        "schema_version": "v1",
        "atom_id": f"s1:scene:{digest}",
        "session_id": "s1",
        "digest": digest,
    }
    scene_atoms = [a for a in atoms.atoms("s1") if a.kind == "scene"]
    assert len(scene_atoms) == 1
    assert scene_atoms[0].source_pipeline_version == "vision"
    # occurred_at = started_at + (3000 - 1000)/1000 = FIXED + 2s
    assert scene_atoms[0].occurred_at == datetime(
        2026, 6, 30, 12, 0, 2, tzinfo=timezone.utc,
    )


async def test_post_snapshot_403_when_vision_disabled():
    _blobs, _vision, _atoms, pipe = _vision_store(b"img1")
    client, _store = _client(pipe=pipe, vision_enabled=False)
    async with client:
        resp = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
            headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 403
    assert body["code"] == "forbidden"
    assert "vision" in body["message"].lower()


async def test_post_snapshot_503_when_vision_not_configured():
    client, _store = _client(pipe=None, vision_enabled=True)
    async with client:
        resp = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
            headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 503
    assert body["code"] == "unavailable"
    assert "vision" in body["message"].lower()


async def test_post_snapshot_401_without_token():
    _blobs, _vision, _atoms, pipe = _vision_store(b"img1")
    client, _store = _client(pipe=pipe)
    async with client:
        resp = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
        )
    assert resp.status == 401


async def test_post_snapshot_idempotent_replay_is_200():
    tl = SessionTimelineIndex()
    tl.record("s1", 1000)
    tl.record("s1", 5000)
    sidx = SessionIndex()
    _seed_session(sidx, "s1", FIXED)
    clock = FakeClock(FIXED)
    _blobs, _vision, _atoms, pipe = _vision_store(b"img1")
    client, _store = _client(
        pipe=pipe, timeline=tl, sidx=sidx, clock=clock,
    )
    async with client:
        resp1 = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
            headers=_AUTH,
        )
        body1 = await resp1.json()
        resp2 = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "3000"},
            headers=_AUTH,
        )
        body2 = await resp2.json()

    assert resp1.status == 201
    assert resp2.status == 200
    assert body1["atom_id"] == body2["atom_id"]


async def test_post_snapshot_unmatched_rel_ts_is_sessionless():
    tl = SessionTimelineIndex()
    tl.record("s1", 1000)
    tl.record("s1", 5000)
    sidx = SessionIndex()
    _seed_session(sidx, "s1", FIXED)
    upload_when = datetime(2026, 7, 1, 9, 30, tzinfo=timezone.utc)
    clock = FakeClock(upload_when)
    _blobs, _vision, atoms, pipe = _vision_store(b"img1")
    client, _store = _client(
        pipe=pipe, timeline=tl, sidx=sidx, clock=clock,
    )
    async with client:
        resp = await client.post(
            "/media/snapshots",
            data=b"img1",
            params={"rel_ts_ms": "99999"},
            headers=_AUTH,
        )
        body = await resp.json()

    assert resp.status == 201
    digest = sha256_hex(b"img1")
    assert body == {
        "schema_version": "v1",
        "atom_id": f"scene:{digest}",
        "session_id": None,
        "digest": digest,
    }
    # The atom is sessionless; occurred_at = upload time.
    scene_atoms = [a for a in atoms.atoms(None) if a.kind == "scene"]
    assert len(scene_atoms) == 1
    assert scene_atoms[0].session_id is None
    assert scene_atoms[0].occurred_at == upload_when


async def test_get_blob_serves_snapshot_image_with_sniffed_content_type():
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    image = b"\xff\xd8\xff\xe0jpeg bytes"
    pipe = VisionPipeline(blobs, FakeVision({image: "x"}), atoms, clock=lambda: FIXED)
    # ingest a snapshot so the blob exists
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    settings_store=InMemorySettingsStore(
                        SettingsDocument.model_validate({"capture": {"vision_enabled": True}})),
                    vision=pipe, blob_store=blobs, session_timeline=SessionTimelineIndex(),
                    session_index=SessionIndex(), clock=FakeClock())
    async with TestClient(TestServer(app)) as client:
        await client.post("/media/snapshots", data=image, params={"rel_ts_ms": "100"},
                           headers=_AUTH)
        resp = await client.get(f"/media/blob/{sha256_hex(image)}", headers=_AUTH)
        assert resp.status == 200
        assert resp.headers["Content-Type"] == "image/jpeg"
        assert await resp.read() == image


async def test_get_blob_404_for_missing_digest():
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    blob_store=InMemoryBlobStore())
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/media/blob/" + "0" * 64, headers=_AUTH)
        assert resp.status == 404
