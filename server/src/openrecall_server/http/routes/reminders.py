"""GET /reminders + POST /reminders/{id}/done (P1 instruction processor).

Lists pending reminders (the phone renders them as a reminders list) and
marks one done. Bearer-auth via the global middleware. The store is
``app["sense_reminders"]`` (wired in build_app).
"""
from __future__ import annotations

from typing import Literal

from aiohttp import web
from pydantic import BaseModel, ConfigDict, Field


class ReminderDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    atom_id: str
    session_id: str
    text: str
    due_at: str
    status: str
    fired_at: str | None = None


class RemindersListDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    reminders: list[ReminderDTO] = Field(default_factory=list)


def _to_dto(r) -> ReminderDTO:
    return ReminderDTO(
        atom_id=r.atom_id, session_id=r.session_id, text=r.text,
        due_at=r.due_at.isoformat(), status=r.status,
        fired_at=r.fired_at.isoformat() if r.fired_at else None,
    )


def add_routes(app: web.Application) -> None:
    app.router.add_get("/reminders", list_reminders)
    app.router.add_post("/reminders/{atom_id}/done", done_reminder)


async def list_reminders(request: web.Request) -> web.Response:
    store = request.app["sense_reminders"]
    if store is None:
        return web.json_response(
            {"code": "internal_error", "message": "reminders not configured"}, status=500,
        )
    rows = store.list(only_pending=True)
    dto = RemindersListDTO(reminders=[_to_dto(r) for r in rows])
    return web.json_response(dto.model_dump(mode="json"))


async def done_reminder(request: web.Request) -> web.Response:
    store = request.app["sense_reminders"]
    if store is None:
        return web.json_response(
            {"code": "internal_error", "message": "reminders not configured"}, status=500,
        )
    atom_id = request.match_info["atom_id"]
    if not store.mark_done(atom_id):
        return web.json_response(
            {"code": "not_found", "message": f"reminder {atom_id!r} not found"}, status=404,
        )
    return web.json_response({"ok": True})