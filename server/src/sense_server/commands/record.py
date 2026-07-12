"""§D command record — durable lifecycle view of one command.

A :class:`CommandRecord` is the canonical view of one command for the
HTTP routes and the Android UI. The underlying :class:`~sense_server.commands.model.Command`
is the wire shape; the record is the storage/lifecycle shape.

The record is built once at issue time and updated on every lifecycle
transition. The ``history`` is append-only (every transition adds a
row); the current ``status`` mirrors the last entry. A redundant
``status`` field on the record makes the common query
("what's the current status?") a single-row read.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .model import Command
from .status import CommandStatus


class StatusTransition(BaseModel):
    """One lifecycle transition: when did the command change
    from :attr:`from_status` to :attr:`to_status`, and why.

    ``detail`` is a free-form dict the dispatcher can populate with
    per-transition context (e.g. ``{"error": "low_battery"}`` on a
    FAILED transition, or ``{"ip": "192.168.1.42"}`` on a DELIVERED
    transition). It shows up in the Android command-detail screen
    so the user can see why a command succeeded or failed.
    """

    model_config = ConfigDict(frozen=True)
    from_status: CommandStatus | None  # None for the initial PENDING entry
    to_status: CommandStatus
    at: datetime
    detail: dict[str, Any] = Field(default_factory=dict)


class CommandRecord(BaseModel):
    """The storage view of one command: the command itself, the
    current lifecycle status, and the append-only history.

    Records are durable: a :class:`SqliteCommandStore` (Phase 7)
    persists them across gateway restarts. The Android command
    lifecycle UI watches ``status`` and ``history`` to render
    pending → executing → completed transitions in real time.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    command: Command
    status: CommandStatus
    history: tuple[StatusTransition, ...] = Field(default_factory=tuple)

    @classmethod
    def at_issue(cls, command: Command) -> "CommandRecord":
        """Build the initial PENDING record for a freshly-issued command.

        The history starts with a single PENDING transition
        (from_status=None) so the lifecycle view is "always at
        least one entry" — empty histories are confusing in the
        Android UI.
        """
        return cls(
            command=command,
            status=CommandStatus.PENDING,
            history=(
                StatusTransition(
                    from_status=None,
                    to_status=CommandStatus.PENDING,
                    at=command.issued_at,
                    detail={"event": "issue"},
                ),
            ),
        )

    def with_transition(
        self,
        to_status: CommandStatus,
        at: datetime,
        detail: dict[str, Any] | None = None,
    ) -> "CommandRecord":
        """Return a new record with an additional history entry.

        The :class:`CommandRecord` is frozen; ``with_transition`` builds
        a fresh record rather than mutating in place (the dispatcher
        stores records, and an in-place mutation would race against
        the in-memory index).
        """
        return self.model_copy(update={
            "status": to_status,
            "history": self.history + (
                StatusTransition(
                    from_status=self.status,
                    to_status=to_status,
                    at=at,
                    detail=detail or {},
                ),
            ),
        })
