"""Phase 5 HTTP — DELETE /segments/{id} (spec §5.2).

A delete the user asked for must actually remove everything derived from the
recording. The failure that matters most is a deleted memory still surfacing
in search, because the user was told it was gone.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.http.app import build_app
from opensapien_server.media.audio import AudioStore
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.index import InMemoryMemoryIndex
from opensapien_server.memory.store import InMemoryAtomStore
from opensapien_server.sessions.segment_meta import InMemorySegmentMetaStore
from opensapien_server.sessions.segments import SEGMENT_IDLE_MS, SegmentIndex

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)
_AUTH = {"Authorization": "Bearer t"}


def _event(seq, *, at=None, ms=1000):
    return CaptureEvent(
        event_id=f"s1:{seq}",
        session_id="s1",
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=f"line {seq}",
        duration_ms=ms,
        start_ms=seq * ms,
    )


def _atom(atom_id, start_ms):
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id="s1:0",
        kind="fact",
        text=f"memory {atom_id}",
        created_at=_T0,
        occurred_at=_T0,
        start_ms=start_ms,
    )


def _client(tmp_path, *, events=(), atoms=(), frames=None, closed=True):
    event_store = InMemoryEventStore()
    for e in events:
        event_store.append(e)
    index = SegmentIndex()
    index.rebuild_from_store(event_store)
    if closed:
        index.close_session("s1")
    atom_store = InMemoryAtomStore()
    memory_index = InMemoryMemoryIndex()
    for a in atoms:
        atom_store.append(a)
        memory_index.add(a, [0.1, 0.2])
    audio = AudioStore(tmp_path / "audio")
    if frames is not None:
        audio.write_at("s1", 0, frames, peaks=[100] * len(frames))
    meta = InMemorySegmentMetaStore()
    meta.set_title("s1:0", "Studio standup", source="user")
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        event_store=event_store,
        segment_index=index,
        segment_meta=meta,
        atom_store=atom_store,
        memory_index=memory_index,
        audio_store=audio,
    )
    return (
        TestClient(TestServer(app)),
        {
            "events": event_store, "index": index, "atoms": atom_store,
            "vectors": memory_index, "audio": audio, "meta": meta,
        },
    )


# ---- the cascade ------------------------------------------------------------


async def test_delete_returns_204_and_removes_the_segment(tmp_path):
    client, dep = _client(tmp_path, events=[_event(0), _event(1)])
    async with client:
        resp = await client.delete("/segments/s1:0", headers=_AUTH)

    assert resp.status == 204
    assert dep["index"].get("s1:0") is None


async def test_delete_removes_the_transcript_events(tmp_path):
    client, dep = _client(tmp_path, events=[_event(0), _event(1)])
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert dep["events"].events("s1") == []


async def test_delete_removes_the_memories_and_their_vectors(tmp_path):
    """An orphaned vector keeps surfacing a memory the user was told was
    deleted — the worst possible outcome for a delete."""
    client, dep = _client(
        tmp_path, events=[_event(0)], atoms=[_atom("a1", 0), _atom("a2", 500)],
    )
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert dep["atoms"].atoms("s1") == []
    assert dep["vectors"].has("a1") is False
    assert dep["vectors"].has("a2") is False


async def test_delete_erases_the_audio_but_keeps_the_timeline(tmp_path):
    """Deleting one recording must not shift the timestamps of every
    recording after it in the same session."""
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(minutes=30)),
    ]
    client, dep = _client(tmp_path, events=events, frames=[b"aa"] * 100)
    before = dep["audio"].stat("s1").slot_count
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert dep["audio"].stat("s1").slot_count == before
    assert list(dep["audio"].read_range("s1", 0, 1000))[:50] == [b""] * 50


async def test_delete_removes_the_title(tmp_path):
    client, dep = _client(tmp_path, events=[_event(0)])
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert dep["meta"].get("s1:0") is None


async def test_delete_removes_the_cached_ogg(tmp_path):
    client, dep = _client(tmp_path, events=[_event(0)], frames=[b"aa"] * 50)
    async with client:
        await client.get("/segments/s1:0/audio", headers=_AUTH)
        cached = dep["audio"].log_path("s1").parent / "seg" / "s1_0.ogg"
        assert cached.exists()
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert not cached.exists()


async def test_a_neighbouring_segment_survives(tmp_path):
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1)),
    ]
    client, dep = _client(tmp_path, events=events)
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    assert dep["index"].get("s1:1") is not None
    assert [e.seq for e in dep["events"].events("s1")] == [1]


# ---- ordering and idempotency -----------------------------------------------


async def test_a_deleted_segment_does_not_come_back_on_rebuild(tmp_path):
    """Events are deleted first precisely so a crash mid-delete cannot
    resurrect the segment from the durable store."""
    client, dep = _client(tmp_path, events=[_event(0), _event(1)])
    async with client:
        await client.delete("/segments/s1:0", headers=_AUTH)

    rebuilt = SegmentIndex()
    rebuilt.rebuild_from_store(dep["events"])

    assert rebuilt.get("s1:0") is None


async def test_delete_is_idempotent(tmp_path):
    client, _dep = _client(tmp_path, events=[_event(0)])
    async with client:
        first = await client.delete("/segments/s1:0", headers=_AUTH)
        second = await client.delete("/segments/s1:0", headers=_AUTH)

    assert first.status == 204
    assert second.status == 404


# ---- refusals ---------------------------------------------------------------


async def test_deleting_an_open_segment_is_409(tmp_path):
    """It is actively being written to, so a delete would race ingest."""
    client, _dep = _client(tmp_path, events=[_event(0)], closed=False)
    async with client:
        resp = await client.delete("/segments/s1:0", headers=_AUTH)

    assert resp.status == 409


async def test_deleting_an_unknown_segment_is_404(tmp_path):
    client, _dep = _client(tmp_path, events=[_event(0)])
    async with client:
        resp = await client.delete("/segments/s1:99", headers=_AUTH)

    assert resp.status == 404


async def test_delete_requires_a_token(tmp_path):
    client, _dep = _client(tmp_path, events=[_event(0)])
    async with client:
        resp = await client.delete("/segments/s1:0")

    assert resp.status == 401
