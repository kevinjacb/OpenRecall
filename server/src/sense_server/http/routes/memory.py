"""GET /memory and GET /sessions/{id}/memory routes (N3.3)."""
from __future__ import annotations

from aiohttp import web

from ...contracts.id_generator import UuidIdGenerator
from ...contracts.types import RetrieverContext
from .dto import (
    MemoryAtomDTO,
    MemorySearchResponseDTO,
    SessionMemoryResponseDTO,
)


def add_routes(app: web.Application) -> None:
    app.router.add_get("/memory", get_memory)
    app.router.add_get("/sessions/{session_id}/memory", get_session_memory)


async def get_memory(request: web.Request) -> web.Response:
    """Search the memory for a free-form query.

    Reserved query params (this slice ignores them but they are part of
    the documented future shape): ``since``, ``until``, ``modality``,
    ``format``.
    """
    query_text = request.query.get("q", "").strip()
    if not query_text:
        return web.json_response(
            {"code": "bad_request", "message": "q is required"}, status=400
        )
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
        start_ms=a.start_ms,
        source_event_id=a.source_event_id,
        source_modality="transcript",
        extraction_version=a.extraction_version,
        embedding_model=a.embedding_model,
        extractor_prompt_version=a.extractor_prompt_version,
    )
