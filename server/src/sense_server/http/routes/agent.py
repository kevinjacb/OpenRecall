"""POST /agent route (N2.2 / INV-8)."""
from __future__ import annotations

import json
from datetime import datetime, timezone

from aiohttp import web

from ...contracts.id_generator import UuidIdGenerator
from ...contracts.types import PlannerContext
from .dto import AgentRequestDTO, AgentResponseDTO, ErrorEnvelopeDTO
from .mapper import map_planner_result_to_dto


def add_routes(app: web.Application) -> None:
    app.router.add_post("/agent", post_agent)


async def post_agent(request: web.Request) -> web.Response:
    """Handle a single ``POST /agent`` request.

    Thin handler: parse the DTO, build the narrow
    :class:`PlannerContext`, delegate to the Planner, map the result
    to the wire DTO. No business logic lives here.
    """
    try:
        body = await request.json()
        dto = AgentRequestDTO.model_validate(body)
    except Exception as e:
        return _bad_request(f"invalid request: {e}", request_id=None)

    # The Planner is the only collaborator; the HTTP layer never
    # imports any domain class besides PlannerContext + the DTOs.
    planner = request.app["sense_planner"]
    request_id = dto.schema_version and _new_request_id(request.app)
    ctx = PlannerContext(
        request_id=request_id,
        trigger_text=dto.text,
        session_id=dto.session_id,
        limit=dto.limit,
    )
    result = await planner.plan(ctx)
    response_dto = map_planner_result_to_dto(result)
    return web.json_response(response_dto.model_dump(mode="json"))


def _new_request_id(app: web.Application) -> str:
    gen = app.get("sense_id_generator") or UuidIdGenerator()
    return gen.new()


def _bad_request(message: str, request_id: str | None) -> web.Response:
    err = ErrorEnvelopeDTO(code="bad_request", message=message, request_id=request_id)
    return web.json_response(err.model_dump(mode="json"), status=400)
