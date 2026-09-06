"""Tests for the read-only MCP tool surface on POST /mcp (spec §5.2)."""
import pytest


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


async def test_device_status_reports_battery_and_capabilities(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id=None, trigger_kind="user_request")
    r = await _call(client, "device.status", {"request_id": "r1"})
    out = (await r.json())["result"]["structuredContent"]
    assert "battery_pct" in out and "capabilities" in out


async def test_mcp_route_rejects_the_relay_token(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "device.status", {"request_id": "r1"}, token="aaa")
    assert r.status == 403
