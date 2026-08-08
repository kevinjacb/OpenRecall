"""Phase 2 HTTP — /segments (spec §2.3, §2.4, §2.5)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.http.app import build_app
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.sessions.segment_meta import InMemorySegmentMetaStore
from openrecall_server.sessions.segments import SEGMENT_IDLE_MS, SegmentIndex

_T0 = datetime(2026, 8, 8, 9, 0, 0, tzinfo=timezone.utc)
_AUTH = {"Authorization": "Bearer t"}


def _event(seq, *, session_id="s1", at=None, text="hello there", ms=1000):
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=at if at is not None else _T0 + timedelta(seconds=seq),
        text=text,
        duration_ms=ms,
        start_ms=seq * ms,
    )


def _atom(atom_id, *, session_id="s1", start_ms=0):
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"{session_id}:0",
        kind="fact",
        text=f"memory {atom_id}",
        created_at=_T0,
        occurred_at=_T0,
        start_ms=start_ms,
    )


def _client(events=(), atoms=(), titles=None):
    event_store = InMemoryEventStore()
    for e in events:
        event_store.append(e)
    index = SegmentIndex()
    index.rebuild_from_store(event_store)
    atom_store = InMemoryAtomStore()
    for a in atoms:
        atom_store.append(a)
    meta = InMemorySegmentMetaStore()
    for segment_id, (title, source) in (titles or {}).items():
        meta.set_title(segment_id, title, source=source)
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        event_store=event_store,
        segment_index=index,
        segment_meta=meta,
        atom_store=atom_store,
    )
    return TestClient(TestServer(app)), index, meta


async def _get(path, **kwargs):
    client, index, meta = _client(**kwargs)
    async with client:
        resp = await client.get(path, headers=_AUTH)
        return resp.status, await resp.json()


# ---- listing ----------------------------------------------------------------


async def test_list_returns_a_segment_row():
    status, body = await _get("/segments", events=[_event(0), _event(1)])

    assert status == 200
    assert body["nextCursor"] is None
    (row,) = body["segments"]
    assert row["id"] == "s1:0"
    assert row["sessionId"] == "s1"
    assert row["transcriptCount"] == 2
    assert row["preview"] == "hello there"
    assert row["title"] is None
    assert row["memoryCount"] == 0
    assert row["hasAudio"] is False
    assert row["durationMs"] == 2000


async def test_list_splits_an_idle_gap_into_two_rows():
    """The list is of recordings, not of relay uptime (spec D1)."""
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1)),
    ]

    status, body = await _get("/segments", events=events)

    assert status == 200
    assert [r["id"] for r in body["segments"]] == ["s1:1", "s1:0"]


async def test_list_shows_the_stored_title():
    status, body = await _get(
        "/segments", events=[_event(0)], titles={"s1:0": ("Studio standup", "llm")},
    )

    assert body["segments"][0]["title"] == "Studio standup"


async def test_list_counts_memories_per_segment():
    """The "4 memories" badge, without the N+1 the gap analysis flagged."""
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1)),
    ]
    atoms = [
        _atom("a1", start_ms=0),      # inside s1:0
        _atom("a2", start_ms=500),    # inside s1:0
        _atom("a3", start_ms=1000),   # inside s1:1
    ]

    status, body = await _get("/segments", events=events, atoms=atoms)

    counts = {r["id"]: r["memoryCount"] for r in body["segments"]}
    assert counts == {"s1:0": 2, "s1:1": 1}


async def test_list_filters_by_session():
    events = [_event(0, session_id="s1"), _event(0, session_id="s2")]

    status, body = await _get("/segments?session_id=s2", events=events)

    assert [r["sessionId"] for r in body["segments"]] == ["s2"]


async def test_list_pages_with_a_cursor():
    # Space them past the idle threshold so each is its own segment.
    events = [
        _event(i * 10, at=_T0 + timedelta(milliseconds=i * (SEGMENT_IDLE_MS + 1000)))
        for i in range(3)
    ]
    client, _index, _meta = _client(events=events)
    async with client:
        first = await (await client.get("/segments?limit=2", headers=_AUTH)).json()
        assert len(first["segments"]) == 2
        assert first["nextCursor"]
        second = await (
            await client.get(
                f"/segments?limit=2&cursor={first['nextCursor']}", headers=_AUTH,
            )
        ).json()

    ids = [r["id"] for r in first["segments"]] + [r["id"] for r in second["segments"]]
    assert ids == ["s1:20", "s1:10", "s1:0"]
    assert second["nextCursor"] is None


async def test_list_rejects_a_malformed_cursor():
    status, body = await _get("/segments?cursor=garbage", events=[_event(0)])

    assert status == 400
    assert body["error"] == "bad_cursor"


async def test_list_requires_a_token():
    client, _index, _meta = _client(events=[_event(0)])
    async with client:
        resp = await client.get("/segments")
    assert resp.status == 401


# ---- search (§2.4) ----------------------------------------------------------


async def test_search_matches_transcript_text():
    events = [
        _event(0, at=_T0, text="the quarterly budget review"),
        _event(1, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1),
               text="lunch plans"),
    ]

    status, body = await _get("/segments?q=budget", events=events)

    assert status == 200
    assert [r["id"] for r in body["segments"]] == ["s1:0"]
    assert "budget" in body["segments"][0]["matchSnippet"]
    assert body["nextCursor"] is None


async def test_search_is_case_insensitive():
    status, body = await _get(
        "/segments?q=BUDGET", events=[_event(0, text="the budget review")],
    )

    assert [r["id"] for r in body["segments"]] == ["s1:0"]


async def test_search_returns_one_row_per_segment():
    """The user is looking for a recording to open, not for every line in it."""
    events = [_event(0, text="budget one"), _event(1, text="budget two")]

    status, body = await _get("/segments?q=budget", events=events)

    assert [r["id"] for r in body["segments"]] == ["s1:0"]


async def test_search_treats_wildcards_as_literal_text():
    """A user typing `50%` must not match every recording."""
    events = [_event(0, text="revenue up 50% this quarter"), _event(1, text="nothing")]

    status, body = await _get("/segments?q=50%", events=events)

    assert [r["id"] for r in body["segments"]] == ["s1:0"]


async def test_search_with_no_hits_returns_an_empty_list():
    status, body = await _get("/segments?q=nonexistent", events=[_event(0)])

    assert status == 200
    assert body["segments"] == []


# ---- detail -----------------------------------------------------------------


async def test_get_segment_returns_summary_and_its_own_events():
    events = [
        _event(0, at=_T0, text="first"),
        _event(1, at=_T0 + timedelta(seconds=1), text="second"),
        _event(2, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1),
               text="later"),
    ]

    status, body = await _get("/segments/s1:0", events=events)

    assert status == 200
    assert body["summary"]["id"] == "s1:0"
    assert [e["text"] for e in body["events"]] == ["first", "second"]


async def test_get_unknown_segment_is_404():
    status, body = await _get("/segments/s1:99", events=[_event(0)])

    assert status == 404
    assert body["error"] == "not_found"


async def test_get_segment_events():
    status, body = await _get("/segments/s1:0/events", events=[_event(0)])

    assert status == 200
    assert [e["id"] for e in body["events"]] == ["s1:0"]


async def test_get_segment_memory_returns_only_atoms_in_range():
    events = [
        _event(0, at=_T0),
        _event(1, at=_T0 + timedelta(seconds=1, milliseconds=SEGMENT_IDLE_MS + 1)),
    ]
    atoms = [_atom("a1", start_ms=0), _atom("a2", start_ms=1000)]

    status, body = await _get("/segments/s1:0/memory", events=events, atoms=atoms)

    assert status == 200
    assert [a["atom_id"] for a in body["atoms"]] == ["a1"]
    assert body["returnedCount"] == 1


# ---- rename (§2.3) ----------------------------------------------------------


async def _patch(path, payload, **kwargs):
    client, _index, meta = _client(**kwargs)
    async with client:
        resp = await client.patch(path, json=payload, headers=_AUTH)
        return resp.status, await resp.json(), meta


async def test_patch_renames_a_segment():
    status, body, meta = await _patch(
        "/segments/s1:0", {"title": "  Studio standup  "}, events=[_event(0)],
    )

    assert status == 200
    assert body["title"] == "Studio standup"
    assert meta.get("s1:0").title == "Studio standup"
    assert meta.get("s1:0").title_source == "user"


async def test_patch_rejects_an_empty_title():
    status, _body, _meta = await _patch(
        "/segments/s1:0", {"title": "   "}, events=[_event(0)],
    )

    assert status == 400


async def test_patch_rejects_an_overlong_title():
    status, _body, _meta = await _patch(
        "/segments/s1:0", {"title": "x" * 200}, events=[_event(0)],
    )

    assert status == 400


async def test_patch_rejects_a_missing_title_field():
    status, _body, _meta = await _patch(
        "/segments/s1:0", {"name": "nope"}, events=[_event(0)],
    )

    assert status == 400


async def test_patch_unknown_segment_is_404():
    status, _body, _meta = await _patch(
        "/segments/s1:99", {"title": "x"}, events=[_event(0)],
    )

    assert status == 404


async def test_patch_requires_a_token():
    client, _index, _meta = _client(events=[_event(0)])
    async with client:
        resp = await client.patch("/segments/s1:0", json={"title": "x"})
    assert resp.status == 401
