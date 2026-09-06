"""The read-only MCP tool surface (spec §5.2).

Every tool takes request_id and is validated against the ledger before it
touches a store. memory.search and memory.get record what they return, which is
what makes Phase 2's provenance gate enforceable.

Deliberately absent: raw audio, speaker embeddings, settings writes,
provisioning, and any delete. A capability Hermes needs is added here with a
guard, never by handing it the relay token.

**INERT UNTIL PHASE 2.** Nothing in `src/` or `scripts/` calls
`RequestLedger.open()`, and the JSON-RPC surface exposes no method that opens
one. A real MCP client can therefore `initialize`, `ping` and `tools/list`, but
every `tools/call` returns `isError: true — request not open`, permanently,
until Phase 2 wires a request-opening path (`agent.respond`, or an equivalent
that opens a ledger entry for the turn). The tool list advertising five tools
is not evidence that any of them can be executed today. This is by design —
the ledger is what makes provenance enforceable, so a call with no scope is
refused rather than served unscoped — but read the list with that in mind.
"""
from __future__ import annotations

from typing import Any

from ..contracts.types import RetrieverContext
from .ledger import LedgerClosedError, RequestLedger
from .protocol import ToolRegistry, ToolSpec

MAX_LIMIT = 20

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

    return reg
