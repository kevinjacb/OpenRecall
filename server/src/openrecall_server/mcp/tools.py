"""The read-only MCP tool surface (spec §5.2).

Every tool takes request_id and is validated against the ledger before it
touches a store. memory.search and memory.get record what they return, which is
what makes Phase 2's provenance gate enforceable.

Deliberately absent: raw audio, speaker embeddings, settings writes,
provisioning, and any delete. A capability Hermes needs is added here with a
guard, never by handing it the relay token.

**STILL UNREACHABLE IN PRACTICE.** Phase 2 shipped the request-opening path:
`agent/hermes_planner.py`'s `HermesPlanner.plan()` calls `RequestLedger.open()`
for every turn, and the six tools registered below (including `agent.respond`)
are real and reachable through that ledger scope. But no real Hermes transport
ships — `scripts/run_gateway.py` raises `SystemExit` for every `[agent]
backend` other than `"planner"` — so no process that boots today ever
constructs a `HermesPlanner` or opens a ledger entry. The operational
conclusion of the original note still holds: read a `tools/list` reply against
a running server with that in mind, because no reachable path opens a ledger
entry, and every `tools/call` on it will refuse with `isError: true — request
not open` until something does.
"""
from __future__ import annotations

from typing import Any

from ..contracts.types import RetrieverContext
from .ledger import (
    AgentResponse, LedgerClosedError, RequestLedger, ResponseAlreadyRecordedError,
)
from .protocol import ToolRegistry, ToolSpec

MAX_LIMIT = 20

# The kinds an agent may claim. Restricted to the two kinds this read-only
# surface can actually validate. issue_command / create_memory /
# create_reminder are server-side MINTS in-process (the planner creates the
# record from the action's text/due_at and produces the id) — there is no
# minting tool out of process, so admitting those kinds here would let an
# agent self-report a write it never performed (e.g. an invented
# memory_atom_id referencing nothing). They return in Phase 4 alongside the
# minting tools that can validate them.
RESPONSE_KINDS = frozenset({"answer", "no_memory"})

_REQUEST_ID_PROP = {
    "request_id": {"type": "string",
                   "description": "The open request this call belongs to."},
}


def _require_open(ledger: RequestLedger, args: dict[str, Any]) -> str:
    request_id = args.get("request_id") or ""
    if ledger.get(request_id) is None:
        raise LedgerClosedError(f"request not open: {request_id!r}")
    return request_id


def _atom_dto(a) -> dict[str, Any]:
    return {
        "atom_id": a.atom_id,
        "session_id": a.session_id,
        "kind": a.kind,
        "text": a.text,
        "created_at": a.created_at.isoformat(),
        "start_ms": a.start_ms,
        "score": getattr(a, "score", None),
    }


def build_registry(*, retriever, atom_store, session_index, speaker_registry,
                   capability_provider, ledger: RequestLedger) -> ToolRegistry:
    reg = ToolRegistry()

    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        request_id = _require_open(ledger, args)
        if retriever is None:
            raise RuntimeError("retrieval is not configured on this server")
        limit = min(int(args.get("limit") or 10), MAX_LIMIT)
        # RetrieverContext.session_id is `str | None = None`, and None means
        # GLOBAL retrieval (contracts/types.py:213). Passing "" would ask for
        # a session literally named empty-string, which matches nothing.
        out = retriever.retrieve(RetrieverContext(
            session_id=args.get("session_id") or None,
            query_text=args["query"],
            limit=limit,
        ))
        atoms = [_atom_dto(a) for a in out.atoms]
        ledger.record_atoms(request_id, [a["atom_id"] for a in atoms])
        return {"atoms": atoms, "trace_id": out.retrieval_trace_id}

    reg.register(ToolSpec(
        name="memory.search",
        description=("Semantic search over the wearer's memory atoms. Returns "
                     "atoms with ids you may later cite."),
        input_schema={
            "type": "object",
            "properties": {
                **_REQUEST_ID_PROP,
                "query": {"type": "string"},
                "session_id": {"type": "string"},
                "limit": {"type": "integer", "maximum": MAX_LIMIT},
            },
            "required": ["request_id", "query"],
        },
        handler=memory_search,
    ))

    async def memory_get(args: dict[str, Any]) -> dict[str, Any]:
        request_id = _require_open(ledger, args)
        if atom_store is None:
            raise RuntimeError("memory is not configured on this server")
        # Capped like memory.search: this is a full scan of the atom store,
        # so an unbounded id list is an unbounded amount of work per call.
        wanted = set(args["atom_ids"][:MAX_LIMIT])
        found = [a for a in atom_store.iter_atoms() if a.atom_id in wanted]
        ledger.record_atoms(request_id, [a.atom_id for a in found])
        return {"atoms": [_atom_dto(a) for a in found]}

    reg.register(ToolSpec(
        name="memory.get",
        description="Fetch specific memory atoms by id.",
        input_schema={
            "type": "object",
            "properties": {
                **_REQUEST_ID_PROP,
                "atom_ids": {"type": "array", "items": {"type": "string"},
                             "maxItems": MAX_LIMIT},
            },
            "required": ["request_id", "atom_ids"],
        },
        handler=memory_get,
    ))

    async def sessions_list(args: dict[str, Any]) -> dict[str, Any]:
        _require_open(ledger, args)
        if session_index is None:
            raise RuntimeError("sessions are not configured on this server")
        limit = min(int(args.get("limit") or 20), 100)
        rows, next_cursor = session_index.list(
            limit=limit, before=args.get("cursor"),
        )
        return {
            "sessions": [
                # SessionSummary's id field is `id`; every other Sense surface
                # calls it session_id, so map it here rather than leaking the
                # index's internal field name to MCP clients.
                {"session_id": s.id,
                 "started_at": s.started_at.isoformat(),
                 "ended_at": s.ended_at.isoformat() if s.ended_at else None,
                 "event_count": s.event_count,
                 "transcript_count": s.transcript_count,
                 "preview": s.preview}
                for s in rows
            ],
            "next_cursor": next_cursor,
        }

    reg.register(ToolSpec(
        name="sessions.list",
        description=("List recent capture sessions, newest first. Pass the "
                     "returned next_cursor to page."),
        input_schema={
            "type": "object",
            "properties": {**_REQUEST_ID_PROP,
                           "limit": {"type": "integer", "maximum": 100},
                           "cursor": {"type": "string"}},
            "required": ["request_id"],
        },
        handler=sessions_list,
    ))

    async def speakers_list(args: dict[str, Any]) -> dict[str, Any]:
        _require_open(ledger, args)
        if speaker_registry is None:
            raise RuntimeError("speaker recognition is not enabled")
        return {"speakers": [
            {"speaker_id": s.speaker_id, "name": s.display_name,
             "is_wearer": s.is_wearer}
            for s in speaker_registry.list_speakers()
        ]}

    reg.register(ToolSpec(
        name="speakers.list",
        description=("Known speakers with their names. Never returns voice "
                     "embeddings."),
        input_schema={"type": "object", "properties": dict(_REQUEST_ID_PROP),
                      "required": ["request_id"]},
        handler=speakers_list,
    ))

    async def device_status(args: dict[str, Any]) -> dict[str, Any]:
        _require_open(ledger, args)
        if capability_provider is None:
            raise RuntimeError("no capability provider configured")
        caps = capability_provider.capabilities()
        res = capability_provider.resources()
        return {
            "capabilities": caps.model_dump(),
            "battery_pct": res.battery_pct,
            "recording": res.recording,
            "relay_connected": res.relay_connected,
        }

    reg.register(ToolSpec(
        name="device.status",
        description="Current wearable status: battery, capabilities, connection.",
        input_schema={"type": "object", "properties": dict(_REQUEST_ID_PROP),
                      "required": ["request_id"]},
        handler=device_status,
    ))

    async def agent_respond(args: dict[str, Any]) -> dict[str, Any]:
        request_id = _require_open(ledger, args)
        kind = args.get("kind")
        if kind not in RESPONSE_KINDS:
            raise ValueError(
                f"unknown kind: {kind!r}; expected one of {sorted(RESPONSE_KINDS)}")
        confidence = args.get("confidence")
        # "confidence" is now in input_schema's "required" list (below): a
        # compliant agent that omits it must be rejected HERE, where it can
        # retry in the same run — not silently reinterpreted downstream by
        # HermesPlanner._gate_confidence as a floor failure under the message
        # "Confidence too low to answer.", which describes a different
        # failure (a low score it DID report) and gives the agent nothing to
        # act on. Refusing at the boundary the agent can act on is this
        # branch's established principle (see the confidence-range check
        # just below, and the provenance/no_memory checks further down).
        if confidence is None:
            raise ValueError(
                "confidence is required: report your actual confidence in "
                "[0, 1] rather than omitting it")
        # input_schema also declares "minimum": 0, "maximum": 1, but
        # protocol.py's dispatch() never validates `arguments` against
        # input_schema — those bounds are decorative. Without this check an
        # out-of-range confidence (e.g. 999.0) sails through and defeats
        # HermesPlanner._gate_confidence's autonomous-answer floor. Mirrors
        # validator.py's ANSWER confidence-bounds rule
        # (CONFIDENCE_OUT_OF_RANGE).
        if not (0.0 <= confidence <= 1.0):
            raise ValueError(
                f"confidence out of range: {confidence!r}; expected [0, 1]")
        # protocol.py's dispatch() never validates `arguments` against
        # input_schema (see above) — nothing upstream guarantees "text" is a
        # string or "atom_ids" a list of strings. A malformed value (e.g.
        # text={"a": 1}) would otherwise be recorded into the ledger here and
        # only raise deep inside HermesPlanner.plan() — AFTER ledger.close(),
        # where FallbackPlanner counts it as a primary failure toward the
        # circuit breaker instead of a tool error the agent could see and fix.
        text_raw = args.get("text")
        if text_raw is not None and not isinstance(text_raw, str):
            raise ValueError(
                f"text must be a string, got {type(text_raw).__name__}")
        atom_ids_raw = args.get("atom_ids")
        if atom_ids_raw is not None and (
            not isinstance(atom_ids_raw, (list, tuple))
            or not all(isinstance(a, str) for a in atom_ids_raw)
        ):
            raise ValueError("atom_ids must be a list of strings")
        atom_ids = tuple(atom_ids_raw or ())
        # The provenance gate: an agent may cite only what THIS request
        # retrieved. Without it, "cite your sources" is a prompt instruction
        # and nothing more.
        uncited = sorted(set(atom_ids) - ledger.cited(request_id))
        if uncited:
            raise ValueError(
                f"uncited atoms (not retrieved by this request): {uncited}")
        text = text_raw or ""
        # Mirrors validator.py's NO_MEMORY handling: a no_memory result is
        # always surfaced with a fixed refusal message, never the agent's own
        # prose (the in-process path never lets action.text reach the user
        # for a REFUSE outcome). Refusing here — rather than silently
        # discarding the text — makes a chatty agent's mistake visible to it
        # as a tool error instead of a silently-dropped write.
        if kind == "no_memory" and text:
            raise ValueError(
                "no_memory must not carry free-form text; leave text empty")
        # Mirrors validator.py rule 2: NO_MEMORY must carry no atom_ids —
        # there is nothing to cite when nothing was found.
        if kind == "no_memory" and atom_ids:
            raise ValueError(
                "no_memory must not carry atom_ids; leave atom_ids empty")
        # Mirrors validator.py's ANSWER-must-cite rule (NO_ATOM_CITED):
        # an answer with zero atom_ids would otherwise reach the planner as
        # indistinguishable from a genuinely-cited one — the citation gate
        # this tool exists to enforce would be vacuous on an empty set.
        # (create_memory / create_reminder / issue_command, which validator.py
        # exempts from this rule, are not in RESPONSE_KINDS at all right now.)
        if kind == "answer" and not atom_ids:
            raise ValueError(
                "answer must cite at least one retrieved atom_id")
        ledger.record_response(request_id, AgentResponse(
            kind=kind,
            text=text,
            atom_ids=atom_ids,
            confidence=confidence,
            command_id=args.get("command_id"),
            memory_atom_id=args.get("memory_atom_id"),
            reminder_id=args.get("reminder_id"),
        ))
        return {"accepted": True, "request_id": request_id}

    reg.register(ToolSpec(
        name="agent.respond",
        description=("Deliver your final answer. Call this exactly once, last. "
                     "You may cite only atom ids returned to you by "
                     "memory.search or memory.get in this same request."),
        input_schema={
            "type": "object",
            "properties": {
                **_REQUEST_ID_PROP,
                "kind": {"type": "string", "enum": sorted(RESPONSE_KINDS)},
                "text": {"type": "string"},
                "atom_ids": {"type": "array", "items": {"type": "string"}},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                "command_id": {"type": "string"},
                "memory_atom_id": {"type": "string"},
                "reminder_id": {"type": "string"},
            },
            "required": ["request_id", "kind", "confidence"],
        },
        handler=agent_respond,
    ))

    return reg
