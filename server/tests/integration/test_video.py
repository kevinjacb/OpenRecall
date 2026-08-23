"""P3 video exit criteria (spec §3.3, sim-only).

The full server-side vision path with no hardware: upload -> scene atom with
vision provenance; vision_enabled gates the upload AND the camera commands via
one capability seam; set_snapshot_interval reconciles; retention sweeps old
image blobs but keeps the atoms; camera_available follows the toggle.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.agent.capability import ReportedCapabilityProvider
from openrecall_server.agent.guardrails_command import StrictCommandGuardrails
from openrecall_server.agent.validator_command import ValidatedCommand
from openrecall_server.commands.dispatcher import CommandDispatcher
from openrecall_server.commands.signing import CommandSigner
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.events.model import CaptureEvent
from openrecall_server.http.app import build_app
from openrecall_server.media.blob import InMemoryBlobStore
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.sessions.index import SessionIndex
from openrecall_server.sessions.timeline import SessionTimelineIndex
from openrecall_server.settings.model import SettingsDocument
from openrecall_server.settings.reconciler import DeviceReconciler
from openrecall_server.settings.store import InMemorySettingsStore
from openrecall_server.vision.pipeline import VisionPipeline
from openrecall_server.vision.retention import sweep_vision_retention

_AUTH = {"Authorization": "Bearer t"}
FIXED = datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc)


class FakeVision:
    def __init__(self, mapping):
        self.mapping = mapping

    def caption(self, image, *, media_type="image/jpeg"):
        return self.mapping[image]


def _seed(sidx, sid, started_at):
    sidx.record(CaptureEvent(
        event_id=f"{sid}-e1", session_id=sid, seq=0, kind="transcript",
        created_at=started_at, text="hello", duration_ms=1000, start_ms=0,
    ))


# ---- 1. upload -> scene atom with vision provenance -------------------------

async def test_upload_creates_scene_atom_with_vision_provenance():
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, FakeVision({b"img": "a cat"}), atoms, clock=lambda: FIXED)
    tl = SessionTimelineIndex(); tl.record("s1", 1000); tl.record("s1", 5000)
    sidx = SessionIndex(); _seed(sidx, "s1", FIXED)
    store = InMemorySettingsStore(SettingsDocument.model_validate(
        {"capture": {"vision_enabled": True}}))
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    settings_store=store, vision=pipe,
                    session_timeline=tl, session_index=sidx, clock=FakeClock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/media/snapshots", data=b"img",
                                 params={"rel_ts_ms": "3000"}, headers=_AUTH)
        body = await resp.json()
    assert resp.status == 201
    scenes = [a for a in atoms.iter_atoms() if a.kind == "scene"]
    assert len(scenes) == 1
    assert scenes[0].to_provenance().source_modality == "vision"
    assert body["session_id"] == "s1"


# ---- 2. vision_disabled refuses upload AND camera commands -----------------

def _validated(command_type, params=None):
    # Mirror tests/agent/test_guardrails_command.py:_command exactly.
    return ValidatedCommand(
        command_type=command_type,  # type: ignore[arg-type]
        params=params or {},
        idempotency_key="ik-1",
        confidence=0.9,
    )


async def test_vision_disabled_refuses_upload_and_camera_commands():
    # upload refused (403)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, FakeVision({b"img": "x"}), atoms, clock=lambda: FIXED)
    store = InMemorySettingsStore(SettingsDocument.model_validate(
        {"capture": {"vision_enabled": False}}))
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    settings_store=store, vision=pipe,
                    session_timeline=SessionTimelineIndex(),
                    session_index=SessionIndex(), clock=FakeClock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/media/snapshots", data=b"img",
                                 params={"rel_ts_ms": "3000"}, headers=_AUTH)
        assert resp.status == 403
    # AND a flush_snapshots command through guardrails built from the provider
    # with vision_enabled=False is refused (camera=False).
    provider = ReportedCapabilityProvider(vision_enabled=lambda: False)
    g = StrictCommandGuardrails(provider.capabilities(), provider.resources())
    cmd = _validated("flush_snapshots")
    assert g.check(cmd).allowed is False


# ---- 3. set_snapshot_interval reconcile cycle ------------------------------

def test_set_snapshot_interval_reconcile_cycle():
    clock = FakeClock()
    dispatcher = CommandDispatcher(CommandSigner.generate(), clock=clock.now)
    settings = InMemorySettingsStore(SettingsDocument.model_validate(
        {"capture": {"snapshot_interval_s": 120}}))
    # Pre-converge audio so the snapshot cadence is the only outstanding work:
    # the reconciler issues one command per cycle (snapshot before audio), so
    # without this the cycle after the snapshot ack would converge audio and
    # return 'start_audio' instead of None. The storm guard under test is the
    # snapshot cadence's, not audio's.
    settings.put_device_state({"audio_enabled": True})
    rec = DeviceReconciler(settings=settings, dispatcher=dispatcher,
                           ids=DeterministicIdGenerator(), clock=clock)
    assert rec.reconcile() == "set_snapshot_interval"
    [cmd] = dispatcher.pending()
    assert cmd.command.params == {"seconds": 120}
    # The reconciler's note_acked updates desired/known state in the settings
    # store; the dispatcher must also be acked so the constant idempotency key
    # 'reconcile:snapshot_interval' can re-issue when desired changes. In the
    # live gateway both acks arrive together from the device's ack frame.
    rec.note_acked("set_snapshot_interval")
    dispatcher.ack(cmd.command.command_id)
    assert rec.reconcile() is None          # snapshot storm guard
    # change desired to 0 (off) -> re-issue
    settings.put(SettingsDocument.model_validate(
        {"capture": {"audio_enabled": True, "sleep_mode": False,
                     "snapshot_interval_s": 0}}))
    assert rec.reconcile() == "set_snapshot_interval"
    [cmd2] = [c for c in dispatcher.pending() if c.command.params == {"seconds": 0}]
    assert cmd2.command.params == {"seconds": 0}


# ---- 4. retention sweeps old image, keeps atom ------------------------------

def test_retention_sweeps_old_image_keeps_atom():
    now = datetime(2026, 8, 22, tzinfo=timezone.utc)
    blobs = InMemoryBlobStore()
    atoms = InMemoryAtomStore()
    old = blobs.put(b"old-img")
    new = blobs.put(b"new-img")
    atoms.append(MemoryAtom(
        atom_id=f"s1:scene:{old}", session_id="s1", source_event_id=f"blob:{old}",
        kind="scene", text="old scene", created_at=now - timedelta(days=40),
        source_pipeline_version="vision", occurred_at=now - timedelta(days=40), start_ms=0))
    atoms.append(MemoryAtom(
        atom_id=f"s1:scene:{new}", session_id="s1", source_event_id=f"blob:{new}",
        kind="scene", text="new scene", created_at=now - timedelta(days=1),
        source_pipeline_version="vision", occurred_at=now - timedelta(days=1), start_ms=0))
    deleted = sweep_vision_retention(atoms, blobs, snapshot_days=30, now=now)
    assert deleted == 1
    assert not blobs.has(old) and blobs.has(new)
    assert len([a for a in atoms.iter_atoms() if a.kind == "scene"]) == 2  # atoms kept


# ---- 5. camera_available follows vision_enabled ----------------------------

async def test_camera_available_follows_vision_enabled():
    on = {"v": True}
    provider = ReportedCapabilityProvider(vision_enabled=lambda: on["v"])
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    capability_provider=provider)
    async with TestClient(TestServer(app)) as client:
        resp = await client.get("/device/status", headers=_AUTH)
        assert (await resp.json())["camera_available"] is True
        on["v"] = False
        resp = await client.get("/device/status", headers=_AUTH)
        assert (await resp.json())["camera_available"] is False


# ---- P4b: clip -> scene atoms + retrieval (binding) ------------------------

def _clip(segments):
    """Deterministic MJPEG clip: list of (grayscale_color, n_frames). Pillow-gated."""
    pytest.importorskip("PIL")
    from PIL import Image
    import io
    out = b""
    for color, n in segments:
        for _ in range(n):
            buf = io.BytesIO()
            Image.new("L", (8, 8), color).save(buf, format="JPEG")
            out += buf.getvalue()
    return out


class _AnyVision:
    """Vision backend that captions any image (keyframe-agnostic)."""

    def __init__(self):
        self.calls = 0

    def caption(self, image, *, media_type="image/jpeg"):
        self.calls += 1
        return "a scene"


async def test_video_clip_creates_scene_atoms_with_vision_provenance():
    blobs = InMemoryBlobStore(); atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, _AnyVision(), atoms, clock=lambda: FIXED)
    store = InMemorySettingsStore(SettingsDocument.model_validate(
        {"capture": {"vision_enabled": True}}))
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    settings_store=store, vision=pipe,
                    blob_store=blobs, session_timeline=SessionTimelineIndex(),
                    session_index=SessionIndex(), clock=FakeClock())
    # 6-frame segments: cuts at frames 6 and 12, both >=5 (keyframe_min_gap)
    # from the prior keyframe -> 3 keyframes: 0, 6, 12. (3-frame segments would
    # put the first cut at frame 3, below the cooldown, yielding only 2.)
    clip = _clip([(60, 6), (200, 6), (0, 6)])
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/media/videos", data=clip,
                                 params={"rel_ts_ms": "1000"}, headers=_AUTH)
        body = await resp.json()
    assert resp.status == 201
    scenes = [a for a in atoms.iter_atoms() if a.kind == "scene"]
    assert len(scenes) == 3
    # Assert source_pipeline_version (not to_provenance().source_modality): the
    # sessionless atoms here have session_id=None, and Provenance.session_id is
    # typed non-optional str, so to_provenance() would raise. The pipeline-version
    # field is what source_modality is derived from, preserving the intent.
    assert all(a.source_pipeline_version == "vision" for a in scenes)
    # each keyframe's blob is retrievable
    for entry in body["atoms"]:
        async with TestClient(TestServer(app)) as c:
            r = await c.get(f"/media/blob/{entry['digest']}", headers=_AUTH)
            assert r.status == 200
            assert r.headers["Content-Type"] == "image/jpeg"


async def test_video_clip_no_change_yields_one_atom():
    blobs = InMemoryBlobStore(); atoms = InMemoryAtomStore()
    pipe = VisionPipeline(blobs, _AnyVision(), atoms, clock=lambda: FIXED)
    store = InMemorySettingsStore(SettingsDocument.model_validate(
        {"capture": {"vision_enabled": True}}))
    app = build_app(token="t", get_pubkey=lambda: b"\x00" * 32,
                    settings_store=store, vision=pipe,
                    blob_store=blobs, clock=FakeClock())
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/media/videos", data=_clip([(128, 8)]),
                                 params={"rel_ts_ms": "1"}, headers=_AUTH)
        assert resp.status == 201
    assert len([a for a in atoms.iter_atoms() if a.kind == "scene"]) == 1