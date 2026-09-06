"""Tests for the read-only MCP tool surface on POST /mcp (spec §5.2)."""
import json

import pytest

from .conftest import SEEDED_SESSION_ID, SEEDED_SPEAKER


async def _call(client, tool, args, token="bbb"):
    return await client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": args},
    }, headers={"Authorization": f"Bearer {token}"})


async def test_tools_list_exposes_the_read_only_surface(mcp_env):
    client, ledger = mcp_env
    r = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1,
                                        "method": "tools/list"},
                          headers={"Authorization": "Bearer bbb"})
    names = {t["name"] for t in (await r.json())["result"]["tools"]}
    assert names == {"memory.search", "memory.get", "sessions.list",
                     "speakers.list", "device.status"}


async def test_memory_search_requires_an_open_request_id(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "memory.search",
                    {"request_id": "never-opened", "query": "roadmap"})
    body = await r.json()
    assert body["result"]["isError"] is True
    assert "request not open" in body["result"]["content"][0]["text"]
    # The guard must run BEFORE the store is touched. `record_atoms` raises
    # the same "request not open" message on the way out, so the message
    # alone would also be produced by a no-op `_require_open`; pinning that
    # retrieval never happened is what makes this test guard the guard.
    assert client.app["sense_retriever"].calls == []


async def test_memory_search_returns_atoms_and_records_citations(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "memory.search",
                    {"request_id": "r1", "query": "roadmap", "limit": 5})
    atoms = (await r.json())["result"]["structuredContent"]["atoms"]
    assert atoms, "expected at least one seeded atom"
    assert ledger.cited("r1") == frozenset(a["atom_id"] for a in atoms)


async def test_memory_search_limit_is_capped_at_20(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "memory.search",
                    {"request_id": "r1", "query": "roadmap", "limit": 500})
    assert len((await r.json())["result"]["structuredContent"]["atoms"]) <= 20


async def test_memory_get_requires_an_open_request_id_and_records_citations(mcp_env):
    client, ledger = mcp_env
    # Guard first: an unopened request must be refused BEFORE the store is
    # scanned. `record_atoms` further down raises the same message, so the
    # message alone would not distinguish a guarded handler from an
    # unguarded one — pin that no scan happened.
    r = await _call(client, "memory.get",
                    {"request_id": "never-opened", "atom_ids": ["a00"]})
    body = await r.json()
    assert body["result"]["isError"] is True
    assert "request not open" in body["result"]["content"][0]["text"]
    assert client.app["sense_atom_store"].scans == 0

    # Then the happy path: the atoms it returns are recorded as citations.
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "memory.get",
                    {"request_id": "r1", "atom_ids": ["a00", "a01"]})
    atoms = (await r.json())["result"]["structuredContent"]["atoms"]
    assert {a["atom_id"] for a in atoms} == {"a00", "a01"}
    assert ledger.cited("r1") == frozenset({"a00", "a01"})


async def test_speakers_list_never_returns_embeddings(mcp_env):
    """The security boundary: speakers.list exposes identity, never biometrics.

    Exact key-set equality, not a `"centroid" not in s` check — a newly added
    leaked field (raw ring buffer, per-speaker vector, anything else) must
    fail this test too, not just the one field we thought of.
    """
    client, ledger = mcp_env
    ledger.open("r1", session_id=None, trigger_kind="user_request")
    r = await _call(client, "speakers.list", {"request_id": "r1"})
    speakers = (await r.json())["result"]["structuredContent"]["speakers"]
    assert speakers, "expected the seeded speaker"
    for s in speakers:
        assert set(s) == {"speaker_id", "name", "is_wearer"}
    assert speakers[0]["speaker_id"] == SEEDED_SPEAKER.speaker_id
    assert speakers[0]["name"] == SEEDED_SPEAKER.display_name
    # Sanity: the fixture's speaker really does carry an embedding, so the
    # key-set assertion above is testing a live leak path and not an
    # accidentally-empty centroid.
    assert SEEDED_SPEAKER.centroid


async def test_sessions_list_maps_summary_id_to_session_id(mcp_env):
    """SessionSummary's field is `id`; every other Sense surface says
    `session_id`, and MCP clients must see the common name."""
    client, ledger = mcp_env
    ledger.open("r1", session_id=None, trigger_kind="user_request")
    r = await _call(client, "sessions.list", {"request_id": "r1"})
    sessions = (await r.json())["result"]["structuredContent"]["sessions"]
    assert sessions, "expected the seeded session"
    assert sessions[0]["session_id"] == SEEDED_SESSION_ID
    assert "id" not in sessions[0]
    assert sessions[0]["transcript_count"] == 1


async def test_device_status_reports_battery_and_capabilities(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id=None, trigger_kind="user_request")
    r = await _call(client, "device.status", {"request_id": "r1"})
    out = (await r.json())["result"]["structuredContent"]
    assert "battery_pct" in out and "capabilities" in out
    # Values, not just keys: ConstantCapabilityProvider is deterministic, so
    # {"battery_pct": None, "capabilities": {}} must not satisfy this test.
    assert out["battery_pct"] == 1.0
    assert out["capabilities"]["microphone"] is True
    assert out["capabilities"]["camera"] is False


@pytest.mark.parametrize("body", [5, "notadict", None, ["notadict"], []])
async def test_malformed_bodies_are_invalid_request_not_500(mcp_env, body):
    """`dispatch` calls `request.get("id")`, so a non-object body would raise
    AttributeError and surface as a 500. JSON-RPC calls that Invalid Request."""
    client, ledger = mcp_env
    # data=, not json=: aiohttp's json=None sends an empty body (a genuine
    # parse error), while this posts the literal JSON `null`.
    r = await client.post("/mcp", data=json.dumps(body),
                          headers={"Authorization": "Bearer bbb",
                                   "Content-Type": "application/json"})
    assert r.status == 200
    out = await r.json()
    errors = out if isinstance(out, list) else [out]
    assert errors, "an empty batch must not answer with an empty array"
    for e in errors:
        assert e["error"]["code"] == -32600


async def test_mcp_route_rejects_the_relay_token(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "device.status", {"request_id": "r1"}, token="aaa")
    assert r.status == 403
