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
                     "speakers.list", "device.status", "agent.respond"}


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
    # Exact key-set equality, not a `"provenance" not in a` check — a newly
    # added leaked field (ScoredAtom.provenance carries session_id,
    # source_event_id and a majority-speaker id) must fail this test too,
    # not just the one field we thought of.
    for a in atoms:
        assert set(a) == {"atom_id", "session_id", "kind", "text",
                          "created_at", "start_ms", "score"}


async def test_memory_search_limit_is_capped_at_20(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "memory.search",
                    {"request_id": "r1", "query": "roadmap", "limit": 500})
    assert len((await r.json())["result"]["structuredContent"]["atoms"]) == 20


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


async def test_speakers_list_requires_an_open_request_id(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "speakers.list", {"request_id": "never-opened"})
    body = await r.json()
    assert body["result"]["isError"] is True
    assert "request not open" in body["result"]["content"][0]["text"]


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


async def test_sessions_list_requires_an_open_request_id(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "sessions.list", {"request_id": "never-opened"})
    body = await r.json()
    assert body["result"]["isError"] is True
    assert "request not open" in body["result"]["content"][0]["text"]


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


async def test_device_status_requires_an_open_request_id(mcp_env):
    client, ledger = mcp_env
    r = await _call(client, "device.status", {"request_id": "never-opened"})
    body = await r.json()
    assert body["result"]["isError"] is True
    assert "request not open" in body["result"]["content"][0]["text"]


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


async def test_agent_respond_accepts_only_retrieved_atoms(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    await _call(client, "memory.search", {"request_id": "r1", "query": "roadmap"})
    cited = sorted(ledger.cited("r1"))
    assert cited, "fixture must retrieve at least one atom"

    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "here you go",
        "atom_ids": [cited[0]], "confidence": 0.9,
    })
    body = (await r.json())["result"]
    assert body["isError"] is False
    assert ledger.response("r1").text == "here you go"


async def test_agent_respond_rejects_uncited_atoms(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "invented",
        "atom_ids": ["never-retrieved"], "confidence": 0.9,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert "never-retrieved" in body["content"][0]["text"]
    assert ledger.response("r1") is None


async def test_agent_respond_answer_requires_at_least_one_atom(mcp_env):
    # Mirrors validator.py's ANSWER-must-cite rule (NO_ATOM_CITED): without
    # this, "uncited atoms" is vacuous on an empty set and a fabricated
    # answer is indistinguishable from a genuinely-cited one.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer",
        "text": "You said the meeting is at 3pm.",
        "atom_ids": [], "confidence": 0.99,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert ledger.response("r1") is None


@pytest.mark.parametrize("confidence", [1.0, 0.0])
async def test_agent_respond_accepts_confidence_at_the_boundaries(
    mcp_env, confidence
):
    # validator.py rule: ANSWER confidence outside [0, 1] ->
    # CONFIDENCE_OUT_OF_RANGE. The boundaries themselves are IN range.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    await _call(client, "memory.search", {"request_id": "r1", "query": "roadmap"})
    cited = sorted(ledger.cited("r1"))
    assert cited, "fixture must retrieve at least one atom"

    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "ok",
        "atom_ids": [cited[0]], "confidence": confidence,
    })
    body = (await r.json())["result"]
    assert body["isError"] is False
    assert ledger.response("r1").confidence == confidence


@pytest.mark.parametrize("confidence", [1.0001, -0.0001, 999.0])
async def test_agent_respond_rejects_out_of_range_confidence(mcp_env, confidence):
    # Demonstrated live: protocol.py's dispatch() never validates `arguments`
    # against input_schema, so the schema's "minimum": 0, "maximum": 1 are
    # decorative — an out-of-range confidence must be rejected here, or it
    # defeats HermesPlanner._gate_confidence's autonomous-answer floor
    # (999.0 >= 0.85 would read as an autonomous RETURN).
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    await _call(client, "memory.search", {"request_id": "r1", "query": "roadmap"})
    cited = sorted(ledger.cited("r1"))
    assert cited, "fixture must retrieve at least one atom"

    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "ok",
        "atom_ids": [cited[0]], "confidence": confidence,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert ledger.response("r1") is None


async def test_agent_respond_rejects_missing_confidence(mcp_env):
    # I1 (inverts the prior test's assertion): a compliant agent that reads
    # the schema literally and omits an "optional-looking" confidence used
    # to have it silently reinterpreted downstream by
    # HermesPlanner._gate_confidence as a floor failure ("Confidence too low
    # to answer.") — losing a correctly-cited answer to a message describing
    # a different failure. "confidence" is now in input_schema's "required"
    # list, and this boundary rejects a missing one directly so the agent
    # can see the real problem and retry in the same run.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    await _call(client, "memory.search", {"request_id": "r1", "query": "roadmap"})
    cited = sorted(ledger.cited("r1"))
    assert cited, "fixture must retrieve at least one atom"

    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "ok",
        "atom_ids": [cited[0]],
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert "confidence" in body["content"][0]["text"]
    assert ledger.response("r1") is None


@pytest.mark.parametrize("bad_text", [{"a": 1}, 5, ["not", "a", "string"]])
async def test_agent_respond_rejects_non_string_text(mcp_env, bad_text):
    # I3: protocol.py's dispatch() never validates `arguments` against
    # input_schema, so the declared "text": {"type": "string"} is decorative
    # — reproduced live by a reviewer passing text={"a": 1} through this
    # boundary: it was recorded into the ledger and only raised a
    # ValidationError deep inside HermesPlanner.plan(), AFTER
    # ledger.close(), where FallbackPlanner counted it as a primary failure
    # toward the circuit breaker instead of a tool error the agent could see
    # and fix. Must be rejected HERE instead.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": bad_text,
        "atom_ids": [], "confidence": 0.9,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert "text" in body["content"][0]["text"]
    assert ledger.response("r1") is None


@pytest.mark.parametrize("bad_atom_ids", [
    "a00",              # a bare string, not a list
    [1, 2, 3],          # a list of the wrong element type
    {"a00": True},      # a dict
])
async def test_agent_respond_rejects_malformed_atom_ids(mcp_env, bad_atom_ids):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "answer", "text": "ok",
        "atom_ids": bad_atom_ids, "confidence": 0.9,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert "atom_ids" in body["content"][0]["text"]
    assert ledger.response("r1") is None


async def test_agent_respond_no_memory_rejects_atom_ids(mcp_env):
    # Mirrors validator.py rule 2: NO_MEMORY must carry no atom_ids.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    await _call(client, "memory.search", {"request_id": "r1", "query": "roadmap"})
    cited = sorted(ledger.cited("r1"))
    assert cited, "fixture must retrieve at least one atom"

    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "no_memory", "text": "",
        "atom_ids": [cited[0]], "confidence": 0.5,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert ledger.response("r1") is None


@pytest.mark.parametrize("kind,extra", [
    ("create_memory", {"memory_atom_id": "atom-1"}),
    ("create_reminder", {"reminder_id": "rem-1"}),
    ("issue_command", {"command_id": "cmd-1"}),
])
async def test_agent_respond_rejects_write_kinds(mcp_env, kind, extra):
    # create_memory / create_reminder / issue_command are server-side MINTS
    # in-process (the planner creates the record from the action's
    # text/due_at and produces the id). Phase 2 exposes no minting tool out
    # of process, so admitting these kinds here would let an agent
    # self-report a write it never performed — e.g. an invented
    # memory_atom_id referencing nothing. They return in Phase 4 alongside
    # the minting tools that can validate them. RESPONSE_KINDS now admits
    # only "answer" and "no_memory", so this must be rejected as an unknown
    # kind, same as any other malformed kind.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    args = {"request_id": "r1", "kind": kind, "text": "some text",
            "atom_ids": [], "confidence": 0.9, **extra}
    r = await _call(client, "agent.respond", args)
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert ledger.response("r1") is None


async def test_agent_respond_is_once_per_request(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    # text="" — a no_memory response with free-form text is rejected on its
    # own terms (see test_agent_respond_rejects_no_memory_with_free_text);
    # this test isolates the once-per-request property instead.
    args = {"request_id": "r1", "kind": "no_memory", "text": "",
            "atom_ids": [], "confidence": 0.5}
    first = await _call(client, "agent.respond", args)
    assert (await first.json())["result"]["isError"] is False
    second = await _call(client, "agent.respond", args)
    assert (await second.json())["result"]["isError"] is True


async def test_agent_respond_rejects_no_memory_with_free_text(mcp_env):
    # Mirrors validator.py's NO_MEMORY handling: no_memory is always
    # surfaced with a fixed refusal message, never the agent's own prose. A
    # chatty agent attaching text to a no_memory result must be refused at
    # the tool boundary, not silently accepted and then dropped downstream.
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "no_memory",
        "text": "I looked but found nothing, sorry!",
        "atom_ids": [], "confidence": 0.5,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert ledger.response("r1") is None


async def test_agent_respond_requires_an_open_request_id(mcp_env):
    client, _ledger = mcp_env
    # atom_ids is non-empty and deliberately NOT `[]`: for an unopened
    # request, `ledger.cited()` returns an empty set regardless, so a
    # no-op `_require_open` would fall through to the uncited-atoms check
    # and raise "uncited atoms ..." instead of "request not open" —
    # `record_response`'s own LedgerClosedError only produces the same
    # "request not open" substring when there is nothing left to check
    # first. A non-empty atom_ids is what makes this test guard the guard.
    r = await _call(client, "agent.respond", {
        "request_id": "never-opened", "kind": "answer", "text": "x",
        "atom_ids": ["some-atom"], "confidence": 0.5,
    })
    body = (await r.json())["result"]
    assert body["isError"] is True
    assert "request not open" in body["content"][0]["text"]


async def test_agent_respond_rejects_an_unknown_kind(mcp_env):
    client, ledger = mcp_env
    ledger.open("r1", session_id="s1", trigger_kind="user_request")
    r = await _call(client, "agent.respond", {
        "request_id": "r1", "kind": "delete_everything", "text": "",
        "atom_ids": [], "confidence": 0.5,
    })
    assert (await r.json())["result"]["isError"] is True

