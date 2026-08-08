"""HTTP /commands routes (P2-commands Phase 8).

Three endpoints, all behind the existing ``bearer_auth_middleware``:

* ``GET /commands`` — list active (non-terminal) commands.
* ``GET /commands/{id}`` — single command with full lifecycle history.
* ``POST /commands/{id}/ack`` — device ack: transitions the command
  from ``DELIVERED`` to ``EXECUTING`` and persists the new state.

The store is the source of truth for the GETs; the dispatcher is
the source of truth for the ack (it runs the state machine, then
writes the result to the store).
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from ...commands.model import Command
from ...commands.record import CommandRecord, StatusTransition
from ...commands.status import CommandStatus


# --- DTOs (the wire format) ------------------------------------------------


class CommandHistoryEntryDto(BaseModel):
    """One lifecycle transition in the command's history.

    Maps to/from :class:`StatusTransition`. The ``from_status`` is
    ``None`` for the initial PENDING entry (the issue-time transition).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    from_status: str | None = None
    to_status: str
    at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_transition(cls, t: StatusTransition) -> "CommandHistoryEntryDto":
        return cls(
            from_status=t.from_status.value if t.from_status else None,
            to_status=t.to_status.value,
            at=t.at,
            detail=dict(t.detail),
        )

    def to_transition(self) -> StatusTransition:
        return StatusTransition(
            from_status=CommandStatus(self.from_status) if self.from_status else None,
            to_status=CommandStatus(self.to_status),
            at=self.at,
            detail=dict(self.detail),
        )


class CommandRecordDto(BaseModel):
    """The wire format for a :class:`CommandRecord`.

    Maps to/from the domain record. The Android UI consumes this
    directly; the audit log reads the same fields.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    command_id: str
    session_id: str
    type: str
    params: dict[str, Any] = Field(default_factory=dict)
    issued_at: datetime
    expires_at: datetime
    status: str
    history: list[CommandHistoryEntryDto] = Field(default_factory=list)

    @classmethod
    def from_record(cls, r: CommandRecord) -> "CommandRecordDto":
        return cls(
            command_id=r.command.command_id,
            session_id=r.command.session_id,
            type=r.command.type,
            params=dict(r.command.params),
            issued_at=r.command.issued_at,
            expires_at=r.command.expires_at,
            status=r.status.value,
            history=[CommandHistoryEntryDto.from_transition(h) for h in r.history],
        )

    def to_record(self) -> CommandRecord:
        return CommandRecord(
            command=Command(
                command_id=self.command_id,
                session_id=self.session_id,
                type=self.type,  # type: ignore[arg-type]
                params=dict(self.params),
                issued_at=self.issued_at,
                expires_at=self.expires_at,
            ),
            status=CommandStatus(self.status),
            history=tuple(h.to_transition() for h in self.history),
        )


def _wire_without_store(signed) -> dict:
    """The command wire shape when no command store is configured.

    ``list_active``/``get`` are the normal source, but the store is an
    optional dependency, and a command that was genuinely issued should not
    fail to serialise just because nothing is persisting records.
    """
    command = signed.command
    return {
        "command_id": command.command_id,
        "session_id": command.session_id,
        "type": command.type,
        "params": dict(command.params),
        "issued_at": command.issued_at.isoformat(),
        "expires_at": command.expires_at.isoformat(),
        "status": CommandStatus.PENDING.value,
        "history": [],
    }


# --- HTTP handlers ----------------------------------------------------------


def add_routes(app) -> None:
    """Register the /commands routes on ``app``.

    The store and dispatcher are injected via ``app[...]`` (the
    existing pattern from provisioning / sessions / agent).
    """
    from aiohttp import web

    async def list_commands(request: web.Request) -> web.Response:
        store = app["sense_command_store"]
        records = store.list_active()
        return web.json_response(
            [CommandRecordDto.from_record(r).model_dump(mode="json") for r in records]
        )

    async def get_command(request: web.Request) -> web.Response:
        store = app["sense_command_store"]
        command_id = request.match_info["command_id"]
        rec = store.get(command_id)
        if rec is None:
            return web.json_response(
                {"code": "not_found", "message": f"unknown command {command_id!r}"},
                status=404,
            )
        return web.json_response(
            CommandRecordDto.from_record(rec).model_dump(mode="json")
        )

    async def post_command_ack(request: web.Request) -> web.Response:
        """Device ack: the wearable tells us a command was delivered.

        Per the spec, ack walks the command from DELIVERED to
        EXECUTING. The state machine enforces valid transitions; an
        invalid one (e.g. ack-ing a COMPLETED command) returns 409.
        """
        store = app["sense_command_store"]
        dispatcher = app["sense_command_dispatcher"]
        command_id = request.match_info["command_id"]
        rec = store.get(command_id)
        if rec is None:
            return web.json_response(
                {"code": "not_found", "message": f"unknown command {command_id!r}"},
                status=404,
            )
        if rec.status != CommandStatus.DELIVERED:
            return web.json_response(
                {
                    "code": "conflict",
                    "message": (
                        f"command {command_id!r} is in status "
                        f"{rec.status.value!r}; ack requires DELIVERED"
                    ),
                },
                status=409,
            )
        # Run the state machine. This raises ValueError on an
        # invalid transition; we already checked DELIVERED above.
        new_rec = rec.with_transition(
            CommandStatus.EXECUTING,
            rec.history[-1].at + __import__("datetime").timedelta(seconds=0),
        )
        store.save(new_rec)
        return web.json_response(
            CommandRecordDto.from_record(new_rec).model_dump(mode="json")
        )

    async def post_command(request: web.Request) -> web.Response:
        """Create a command (spec §4.3, blocking gap #5).

        Until now commands could only be created by the agent's internal
        ``issue_command`` path, so the Settings toggles had nothing to call.
        This goes through the same validate → guardrails → sign chain
        (:func:`commands.issue.validate_and_issue`) rather than a parallel
        one — a second copy of the safety chain is a copy that drifts.

        ``session_id`` is accepted but should be omitted: an HTTP caller has
        no truthful value for it (spec D3), and an empty value means "the
        device, whenever it is next connected".
        """
        from ...commands.issue import validate_and_issue

        dispatcher = app["sense_command_dispatcher"]
        ids = app["sense_id_generator"]
        clock = app["sense_clock"]
        if dispatcher is None or ids is None or clock is None:
            return web.json_response(
                {"code": "not_found", "message": "command dispatch is not configured"},
                status=404,
            )
        try:
            body = await request.json()
        except Exception:
            return web.json_response(
                {"code": "bad_request", "message": "body must be JSON"}, status=400,
            )
        if not isinstance(body, dict):
            return web.json_response(
                {"code": "bad_request", "message": "body must be an object"}, status=400,
            )
        command_type = body.get("type")
        idempotency_key = body.get("idempotency_key")
        if not isinstance(command_type, str) or not command_type:
            return web.json_response(
                {"code": "bad_request", "message": "type is required"}, status=400,
            )
        if not isinstance(idempotency_key, str) or not idempotency_key:
            # Required, not generated: a caller that retries a failed request
            # without a stable key would issue the command twice, and only
            # the caller knows which retries are the "same" request.
            return web.json_response(
                {"code": "bad_request", "message": "idempotency_key is required"},
                status=400,
            )
        params = body.get("params") or {}
        if not isinstance(params, dict):
            return web.json_response(
                {"code": "bad_request", "message": "params must be an object"},
                status=400,
            )

        store = app["sense_command_store"]
        # Asked *before* issuing: afterwards the dispatcher has absorbed the
        # duplicate and both cases look identical from here.
        replayed = dispatcher.find_by_idempotency_key(idempotency_key)

        result = validate_and_issue(
            command_type=command_type,
            params=params,
            idempotency_key=idempotency_key,
            dispatcher=dispatcher,
            ids=ids,
            clock=clock,
            capability_provider=app.get("sense_capability_provider"),
            session_id=body.get("session_id") or "",
        )
        if not result.ok:
            # A validation failure means the request was malformed (400); a
            # guardrail refusal means the device can't do it right now (403).
            # Collapsing them would leave a client unable to tell "fix your
            # request" from "try again later".
            status = 403 if result.stage == "guardrails" else 400
            if result.stage == "dispatch":
                status = 500
            return web.json_response(
                {
                    "code": (
                        "forbidden" if status == 403
                        else "internal_error" if status == 500
                        else "bad_request"
                    ),
                    "message": result.message or "command rejected",
                    "reason": result.rejection.value if result.rejection else None,
                },
                status=status,
            )

        rec = store.get(result.signed.command.command_id) if store is not None else None
        payload = (
            CommandRecordDto.from_record(rec).model_dump(mode="json")
            if rec is not None
            else _wire_without_store(result.signed)
        )
        # An idempotent replay returns the original command at 200; a genuinely
        # new command is 201. The distinction lets a client tell "your retry
        # was absorbed" from "a second command is now in flight".
        is_replay = (
            replayed is not None
            and replayed == result.signed.command.command_id
        )
        return web.json_response(payload, status=200 if is_replay else 201)

    app.router.add_post("/commands", post_command)
    app.router.add_get("/commands", list_commands)
    app.router.add_get("/commands/{command_id}", get_command)
    app.router.add_post("/commands/{command_id}/ack", post_command_ack)
