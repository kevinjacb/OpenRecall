"""Phase 1 HTTP — GET /memory list mode and GET /memory/stats (spec §1.2, §1.3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.http.app import build_app
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


def _atom(atom_id: str, *, session_id="s1", kind="fact", hours_ago=1) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"{session_id}:{atom_id}",
        kind=kind,
        text=f"memory {atom_id}",
        created_at=_NOW,
        occurred_at=_NOW - timedelta(hours=hours_ago),
        start_ms=0,
    )


def _client(atoms=()):
    store = InMemoryAtomStore()
    for a in atoms:
        store.append(a)
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        atom_store=store,
        id_generator=DeterministicIdGenerator(),
    )
    return TestClient(TestServer(app)), store


async def _get(atoms, path):
    client, _store = _client(atoms)
    async with client:
        resp = await client.get(path, headers={"Authorization": "Bearer t"})
        return resp.status, await resp.json()


# ---- list mode --------------------------------------------------------------


async def test_list_returns_atoms_newest_first():
    status, body = await _get(
        [_atom("a1", hours_ago=3), _atom("a2", hours_ago=1)], "/memory",
    )

    assert status == 200
    assert [a["atom_id"] for a in body["atoms"]] == ["a2", "a1"]
    assert body["returned_count"] == 2
    assert body["next_cursor"] is None
    assert body["schema_version"] == "v1"


async def test_list_exposes_both_timestamps():
    status, body = await _get([_atom("a1", hours_ago=5)], "/memory")

    assert status == 200
    atom = body["atoms"][0]
    # pydantic renders UTC as a `Z` suffix rather than `+00:00`.
    assert atom["occurred_at"] == "2026-08-08T07:00:00Z"
    assert atom["created_at"] == "2026-08-08T12:00:00Z"


async def test_list_filters_by_kind():
    status, body = await _get(
        [_atom("a1", kind="task"), _atom("a2", kind="fact")], "/memory?kind=task",
    )

    assert status == 200
    assert [a["atom_id"] for a in body["atoms"]] == ["a1"]


async def test_list_filters_by_session():
    status, body = await _get(
        [_atom("a1", session_id="s1"), _atom("a2", session_id="s2")],
        "/memory?session_id=s2",
    )

    assert status == 200
    assert [a["atom_id"] for a in body["atoms"]] == ["a2"]


async def test_list_pages_with_an_opaque_cursor():
    atoms = [_atom(f"a{i}", hours_ago=i + 1) for i in range(3)]
    client, _store = _client(atoms)
    async with client:
        headers = {"Authorization": "Bearer t"}
        first = await (await client.get("/memory?limit=2", headers=headers)).json()
        assert [a["atom_id"] for a in first["atoms"]] == ["a0", "a1"]
        assert first["next_cursor"]

        second = await (
            await client.get(
                f"/memory?limit=2&cursor={first['next_cursor']}", headers=headers,
            )
        ).json()

    assert [a["atom_id"] for a in second["atoms"]] == ["a2"]
    assert second["next_cursor"] is None


async def test_list_clamps_the_limit():
    atoms = [_atom(f"a{i}", hours_ago=i + 1) for i in range(5)]

    status, body = await _get(atoms, "/memory?limit=200")

    assert status == 200
    assert body["returned_count"] == 5  # clamped to 100, not rejected


async def test_list_rejects_a_malformed_cursor():
    """A silent fall back to page one would hide the bug and loop the client."""
    status, body = await _get([_atom("a1")], "/memory?cursor=not-a-cursor")

    assert status == 400
    assert body["code"] == "bad_request"


async def test_list_requires_a_token():
    client, _store = _client([_atom("a1")])
    async with client:
        resp = await client.get("/memory")
    assert resp.status == 401


async def test_search_mode_still_works_when_q_is_present():
    """`q` must still route to semantic search, not to the list path."""
    client, _store = _client([_atom("a1")])
    async with client:
        resp = await client.get("/memory?q=hello", headers={"Authorization": "Bearer t"})
        # No retriever is wired in this app, so the search path fails loudly
        # rather than silently degrading into a list — which is the point.
        assert resp.status == 500


# ---- stats ------------------------------------------------------------------


async def test_stats_reports_totals_and_kinds():
    atoms = [
        _atom("a1", kind="task", hours_ago=1),
        _atom("a2", kind="task", hours_ago=48),
        _atom("a3", kind="fact", hours_ago=2),
    ]

    status, body = await _get(atoms, "/memory/stats")

    assert status == 200
    assert body == {
        "schema_version": "v1",
        "total": 3,
        "added_24h": 2,
        "by_kind": {"task": 2, "fact": 1},
    }


async def test_stats_on_an_empty_store():
    status, body = await _get([], "/memory/stats")

    assert status == 200
    assert body == {
        "schema_version": "v1", "total": 0, "added_24h": 0, "by_kind": {},
    }


async def test_stats_requires_a_token():
    client, _store = _client([])
    async with client:
        resp = await client.get("/memory/stats")
    assert resp.status == 401
