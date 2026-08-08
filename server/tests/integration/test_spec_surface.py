"""Integration — the whole spec surface on one fully-wired app.

The per-phase tests each build a minimal app with only the dependencies that
phase needs. This one wires everything at once, so a dependency that two
phases disagree about (or a route that only works when something else is
absent) shows up here rather than in production.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from opensapien_server.agent.capability import ConstantCapabilityProvider
from opensapien_server.commands.dispatcher import CommandDispatcher
from opensapien_server.commands.signing import CommandSigner
from opensapien_server.commands.store import SqliteCommandStore
from opensapien_server.contracts.clock import FakeClock
from opensapien_server.contracts.id_generator import DeterministicIdGenerator
from opensapien_server.contracts.types import DeviceResourceStatus
from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.gateway.liveness import DeviceLiveness
from opensapien_server.http.app import build_app
from opensapien_server.media.audio import AudioStore
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.index import InMemoryMemoryIndex
from opensapien_server.memory.store import InMemoryAtomStore
from opensapien_server.sessions.index import SessionIndex
from opensapien_server.sessions.lifecycle import SessionLifecycle
from opensapien_server.sessions.segment_meta import InMemorySegmentMetaStore
from opensapien_server.sessions.segments import SEGMENT_IDLE_MS, SegmentIndex
from opensapien_server.settings.reconciler import DeviceReconciler
from opensapien_server.settings.store import InMemorySettingsStore

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)
_AUTH = {"Authorization": "Bearer t"}


def _event(seq, *, at=None, text="hello there", ms=1000):
    return CaptureEvent(
        event_id=f"s1:{seq}",
        session_id="s1",
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=text,
        duration_ms=ms,
        start_ms=seq * ms,
    )


def _atom(atom_id, *, kind="fact", start_ms=0):
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id="s1:0",
        kind=kind,
        text=f"memory {atom_id}",
        created_at=_T0,
        occurred_at=_T0,
        start_ms=start_ms,
    )


@pytest.fixture
async def wired(tmp_path):
    """One app with every spec dependency wired in.

    Async because ``TestClient`` binds a cookie jar to the running loop at
    construction time.
    """
    events = InMemoryEventStore()
    for e in [
        _event(0, at=_T0, text="the quarterly budget review"),
        _event(1, at=_T0 + timedelta(seconds=1), text="and then lunch"),
        _event(2, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1),
               text="afternoon retro"),
    ]:
        events.append(e)

    segment_index = SegmentIndex()
    segment_index.rebuild_from_store(events)
    segment_index.close_session("s1")

    session_index = SessionIndex()
    session_index.rebuild_from_store(events)

    atoms = InMemoryAtomStore()
    vectors = InMemoryMemoryIndex()
    for a in [_atom("a1", kind="task", start_ms=0), _atom("a2", start_ms=2000)]:
        atoms.append(a)
        vectors.add(a, [0.1, 0.2])

    audio = AudioStore(tmp_path / "audio")
    audio.write_at("s1", 0, [b"aa"] * 200, peaks=[128] * 200)

    meta = InMemorySegmentMetaStore()
    meta.set_title("s1:0", "Budget review", source="llm")

    settings = InMemorySettingsStore()
    clock = FakeClock()
    command_store = SqliteCommandStore(tmp_path / "commands.db")
    dispatcher = CommandDispatcher(
        CommandSigner.generate(), clock=clock.now, store=command_store,
    )
    liveness = DeviceLiveness()
    liveness.connection_opened()
    liveness.packet_received()
    lifecycle = SessionLifecycle()
    lifecycle.register("s1")

    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        event_store=events,
        session_index=session_index,
        session_lifecycle=lifecycle,
        segment_index=segment_index,
        segment_meta=meta,
        atom_store=atoms,
        memory_index=vectors,
        audio_store=audio,
        settings_store=settings,
        liveness=liveness,
        reconciler=DeviceReconciler(
            settings=settings, dispatcher=dispatcher,
            ids=DeterministicIdGenerator(prefix="rec"), clock=clock,
        ),
        capability_provider=ConstantCapabilityProvider(
            resources=DeviceResourceStatus(relay_connected=True),
        ),
        command_store=command_store,
        command_dispatcher=dispatcher,
        id_generator=DeterministicIdGenerator(),
        clock=clock,
    )
    return TestClient(TestServer(app)), {
        "events": events, "segments": segment_index, "atoms": atoms,
        "vectors": vectors, "audio": audio, "meta": meta, "settings": settings,
        "dispatcher": dispatcher,
    }


async def test_every_spec_endpoint_answers(wired):
    """One pass over the surface the design needs. A route that only works
    in isolation fails here."""
    client, _dep = wired
    async with client:
        for path in [
            "/health",
            "/status",
            "/memory",
            "/memory/stats",
            "/segments",
            "/segments?q=budget",
            "/segments/s1:0",
            "/segments/s1:0/events",
            "/segments/s1:0/memory",
            "/segments/s1:0/audio",
            "/segments/s1:0/waveform",
            "/settings",
            "/device/status",
            "/sessions",
            "/commands",
        ]:
            resp = await client.get(path, headers=_AUTH)
            assert resp.status == 200, f"{path} returned {resp.status}"


async def test_every_spec_endpoint_requires_the_token(wired):
    """The global middleware covers new routes automatically, but "covers
    automatically" is a claim worth checking once against the real list."""
    client, _dep = wired
    async with client:
        for method, path in [
            ("get", "/memory"),
            ("get", "/memory/stats"),
            ("get", "/segments"),
            ("get", "/segments/s1:0"),
            ("get", "/segments/s1:0/audio"),
            ("get", "/segments/s1:0/waveform"),
            ("patch", "/segments/s1:0"),
            ("delete", "/segments/s1:0"),
            ("get", "/settings"),
            ("put", "/settings"),
            ("get", "/device/status"),
            ("post", "/commands"),
        ]:
            resp = await getattr(client, method)(path)
            assert resp.status == 401, f"{method} {path} returned {resp.status}"


async def test_the_recordings_page_renders_from_one_call(wired):
    """Home and Recordings need title, duration, memory count and audio
    availability per row — the fields the gap analysis said required N+1
    calls or did not exist."""
    client, _dep = wired
    async with client:
        body = await (await client.get("/segments", headers=_AUTH)).json()

    row = next(r for r in body["segments"] if r["id"] == "s1:0")
    assert row["title"] == "Budget review"
    assert row["transcriptCount"] == 2
    assert row["memoryCount"] == 1
    assert row["hasAudio"] is True
    assert row["durationMs"] > 0


async def test_the_memories_page_renders_from_two_calls(wired):
    client, _dep = wired
    async with client:
        listing = await (await client.get("/memory", headers=_AUTH)).json()
        stats = await (await client.get("/memory/stats", headers=_AUTH)).json()
        filtered = await (await client.get("/memory?kind=task", headers=_AUTH)).json()

    assert stats["total"] == 2
    assert set(stats["by_kind"]) == {"task", "fact"}
    assert len(listing["atoms"]) == 2
    assert [a["atom_id"] for a in filtered["atoms"]] == ["a1"]


async def test_a_settings_toggle_reaches_the_command_queue(wired):
    """The full D2/D3 path: a toggle writes desired state, the reconciler
    issues an unbound command, and it is waiting for whatever session
    connects next."""
    client, dep = wired
    async with client:
        resp = await client.put(
            "/settings", json={"capture": {"audio_enabled": False}}, headers=_AUTH,
        )

    assert resp.status == 200
    (pending,) = dep["dispatcher"].pending()
    assert pending.command.type == "stop_audio"
    assert pending.command.session_id == ""  # unbound (D3)


async def test_a_rename_survives_into_the_listing(wired):
    client, _dep = wired
    async with client:
        await client.patch(
            "/segments/s1:0", json={"title": "Q3 planning"}, headers=_AUTH,
        )
        body = await (await client.get("/segments", headers=_AUTH)).json()

    row = next(r for r in body["segments"] if r["id"] == "s1:0")
    assert row["title"] == "Q3 planning"


async def test_a_delete_removes_the_row_and_its_memories(wired):
    client, dep = wired
    async with client:
        resp = await client.delete("/segments/s1:0", headers=_AUTH)
        listing = await (await client.get("/segments", headers=_AUTH)).json()

    assert resp.status == 204
    assert [r["id"] for r in listing["segments"]] == ["s1:2"]
    assert dep["vectors"].has("a1") is False


async def test_device_status_separates_measured_from_placeholder(wired):
    client, _dep = wired
    async with client:
        body = await (await client.get("/device/status", headers=_AUTH)).json()

    assert body["source"] == "static"
    assert body["battery_pct"] == 0.45      # placeholder, flagged
    assert body["relay_connected"] is True  # measured
    assert body["recording"] is True        # measured
    assert body["last_packet_at"] is not None
