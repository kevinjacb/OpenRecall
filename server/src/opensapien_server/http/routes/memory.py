"""GET /memory, GET /memory/stats and GET /sessions/{id}/memory routes.

``GET /memory`` serves two modes off one path, chosen by the presence of
``q``:

* **``q`` present** — semantic search (the original N3.3 behaviour), ranked
  by embedding similarity and carrying a retrieval trace.
* **``q`` absent** — a browsable, filterable, paged list (spec §1.2). This
  is what the Memories tab renders; before it existed the endpoint returned
  400 on an empty query and the page could not be built at all.

They share a path because they are the same collection seen two ways, and
the client's "clear the search box" gesture should not have to switch
endpoints. They do not share a response shape — see
:class:`MemoryListResponseDTO`.
"""
from __future__ import annotations

from aiohttp import web

from ...contracts.id_generator import UuidIdGenerator
from ...contracts.types import RetrieverContext
from ...memory.store import LIST_LIMIT_DEFAULT, LIST_LIMIT_MAX
from ...paging import decode_cursor, encode_cursor
from .dto import (
    MemoryAtomDTO,
    MemoryListResponseDTO,
    MemorySearchResponseDTO,
    MemoryStatsResponseDTO,
    SessionMemoryResponseDTO,
)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/memory", get_memory)
    app.router.add_get("/memory/stats", get_memory_stats)
    app.router.add_get("/sessions/{session_id}/memory", get_session_memory)


def _error(code: str, message: str, status: int) -> web.Response:
    return web.json_response({"code": code, "message": message}, status=status)


def _clamp_limit(raw: str | None, *, default: int, maximum: int) -> int:
    if not raw:
        return default
    try:
        n = int(raw)
    except ValueError:
        return default
    return max(1, min(n, maximum))


async def get_memory(request: web.Request) -> web.Response:
    """Search (``?q=``) or list (no ``q``) the memory atoms."""
    query_text = request.query.get("q", "").strip()
    if not query_text:
        return await _list_memory(request)
    return await _search_memory(request, query_text)


async def _list_memory(request: web.Request) -> web.Response:
    """List mode (spec §1.2) — ``?kind=&session_id=&limit=&cursor=``.

    Ordered by conversation time descending. ``kind`` is matched against the
    raw stored value with no validation: the extractor's kind vocabulary is
    free-form, so validating against a fixed list here would 400 on kinds
    that genuinely exist in the database. An unknown kind returns an empty
    page, which is the honest answer.
    """
    atom_store = request.app["sense_atom_store"]
    if atom_store is None:
        return _error("not_found", "no atom store", 404)

    limit = _clamp_limit(
        request.query.get("limit"), default=LIST_LIMIT_DEFAULT, maximum=LIST_LIMIT_MAX,
    )
    cursor = request.query.get("cursor")
    before = None
    if cursor:
        try:
            before = decode_cursor(cursor)
        except ValueError:
            return _error("bad_request", "malformed cursor", 400)

    atoms, next_anchor = atom_store.list(
        kind=request.query.get("kind") or None,
        session_id=request.query.get("session_id") or None,
        limit=limit,
        before=before,
    )
    dto = MemoryListResponseDTO(
        atoms=[_to_dto_from_store(a) for a in atoms],
        returned_count=len(atoms),
        next_cursor=(
            encode_cursor(before=next_anchor[0], last_id=next_anchor[1])
            if next_anchor is not None
            else None
        ),
    )
    return web.json_response(dto.model_dump(mode="json"))


async def get_memory_stats(request: web.Request) -> web.Response:
    """Total / last-24h / per-kind atom counts (spec §1.3)."""
    atom_store = request.app["sense_atom_store"]
    if atom_store is None:
        return _error("not_found", "no atom store", 404)
    stats = atom_store.stats()
    dto = MemoryStatsResponseDTO(
        total=stats.total, added_24h=stats.added_24h, by_kind=stats.by_kind,
    )
    return web.json_response(dto.model_dump(mode="json"))


async def _search_memory(request: web.Request, query_text: str) -> web.Response:
    """Search mode — the original semantic retrieval path.

    Reserved query params (this slice ignores them but they are part of
    the documented future shape): ``since``, ``until``, ``modality``,
    ``format``.
    """
    session_id = request.query.get("session_id") or None
    try:
        limit = int(request.query.get("limit", "10"))
    except ValueError:
        limit = 10

    retriever = request.app["sense_retriever"]
    id_gen = request.app.get("sense_id_generator") or UuidIdGenerator()
    request_id = id_gen.new()
    rc = retriever.retrieve(RetrieverContext(
        query_text=query_text,
        limit=limit,
        session_id=session_id,
    ))
    dto = MemorySearchResponseDTO(
        request_id=request_id,
        retrieval_trace_id=rc.retrieval_trace_id,
        audit_id="",
        query=query_text,
        session_id=session_id,
        atoms=[_to_dto(a) for a in rc.atoms],
        returned_count=rc.returned_count,
        top_score=rc.top_score if rc.top_score != float("-inf") else 0.0,
        retrieval_latency_ms=rc.retrieval_latency_ms,
    )
    return web.json_response(dto.model_dump(mode="json"))


async def get_session_memory(request: web.Request) -> web.Response:
    """List the atoms for one session, ordered by created_at."""
    session_id = request.match_info["session_id"]
    atom_store = request.app["sense_atom_store"]
    if atom_store is None:
        return web.json_response(
            {"code": "not_found", "message": "no atom store"}, status=404
        )
    atoms = atom_store.atoms(session_id)
    dto = SessionMemoryResponseDTO(
        session_id=session_id,
        atoms=[_to_dto_from_store(a) for a in atoms],
        returned_count=len(atoms),
    )
    return web.json_response(dto.model_dump(mode="json"))


def _to_dto(a) -> MemoryAtomDTO:
    prov = a.provenance
    return MemoryAtomDTO(
        atom_id=a.atom_id,
        session_id=a.session_id,
        kind=a.kind,
        text=a.text,
        created_at=a.created_at,
        occurred_at=getattr(a, "occurred_at", None),
        start_ms=a.start_ms,
        source_event_id=prov.source_event_id if prov else "",
        source_modality=(prov.source_modality if prov else "transcript"),
        extraction_version=(prov.extraction_version if prov else "v1"),
        embedding_model=(prov.embedding_model if prov else ""),
        extractor_prompt_version=(prov.extractor_prompt_version if prov else "v1"),
    )


def _to_dto_from_store(a) -> MemoryAtomDTO:
    return MemoryAtomDTO(
        atom_id=a.atom_id,
        session_id=a.session_id,
        kind=a.kind,
        text=a.text,
        created_at=a.created_at,
        occurred_at=a.occurred_at,
        start_ms=a.start_ms,
        source_event_id=a.source_event_id,
        source_modality="transcript",
        extraction_version=a.extraction_version,
        embedding_model=a.embedding_model,
        extractor_prompt_version=a.extractor_prompt_version,
    )
