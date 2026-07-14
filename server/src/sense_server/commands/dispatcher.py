"""Command dispatcher: at-least-once delivery with idempotent acks and expiry.

Signs and tracks outstanding commands. :meth:`pending` returns the commands still
needing delivery (issued, not yet acked, not yet expired) so a transport can
re-offer them until they land — the wearable executes each ``command_id`` at most
once, so re-delivery is safe. Acks are idempotent, and issuing a duplicate
``command_id`` is a no-op.

P2-commands Phase 4: adds ``idempotency_key`` dedup. Two issues with the
same key produce the same ``command_id``; a subsequent issue after the
original is acked is treated as a NEW issue (the previous one
completed; the user asked again).

P2-commands Phase 5: lifecycle state machine. The dispatcher drives
each command through the binding state machine
(``commands.status``) via :meth:`transition`, raising on invalid
transitions. The audit log records every transition (Phase 7
persists to a ``command_records`` table).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Callable

from .model import Command
from .signing import CommandSigner, SignedCommand
from .record import StatusTransition
from .record import CommandRecord
from .status import CommandStatus, is_valid_transition

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CommandDispatcher:
    def __init__(
        self,
        signer: CommandSigner,
        clock: Clock = _utcnow,
        store: "SqliteCommandStore | None" = None,
    ) -> None:
        self._signer = signer
        self._clock = clock
        self._store = store  # optional: persist records on every operation
        # command_id -> signed (issue order)
        self._signed: dict[str, SignedCommand] = {}
        # idempotency_key -> command_id (for dedup across retries)
        self._idem: dict[str, str] = {}
        self._acked: set[str] = set()
        # Lifecycle state: command_id -> current status.
        self._status: dict[str, CommandStatus] = {}
        # Lifecycle history: command_id -> ordered list of transitions.
        # The list starts with the initial PENDING entry at issue time
        # so the Android UI never sees an empty history.
        self._history: dict[str, list[StatusTransition]] = {}

    def issue(self, command: Command) -> SignedCommand:
        """Sign and track a command.

        Idempotency (P2-commands Phase 4): if ``command.idempotency_key``
        is set and we already have an UNACKED command with that key,
        return the original signed command (a no-op). If the original
        is acked, this is a NEW issue (the previous one completed).
        Without ``idempotency_key`` we fall back to dedup by
        ``command_id`` (the original Phase 1 behavior).
        """
        # idempotency_key dedup.
        if command.idempotency_key:
            existing_id = self._idem.get(command.idempotency_key)
            if existing_id is not None and existing_id not in self._acked:
                existing = self._signed.get(existing_id)
                if existing is not None:
                    return existing
        # command_id dedup (Phase 1 / legacy).
        existing = self._signed.get(command.command_id)
        if existing is not None:
            return existing
        signed = self._signer.sign(command)
        self._signed[command.command_id] = signed
        if command.idempotency_key:
            self._idem[command.idempotency_key] = command.command_id
        # Initial PENDING entry. The history is the contract for the
        # Android lifecycle view; the audit log reads it.
        now = self._clock()
        self._status[command.command_id] = CommandStatus.PENDING
        self._history[command.command_id] = [
            StatusTransition(
                from_status=None,
                to_status=CommandStatus.PENDING,
                at=now,
                detail={"event": "issue"},
            )
        ]
        if self._store is not None:
            self._store.save(self._record(command.command_id))
        return signed

    def _record(self, command_id: str) -> 'CommandRecord':
        if command_id not in self._signed:
            raise KeyError(f'unknown command {command_id!r}')
        return CommandRecord(
            command=self._signed[command_id].command,
            status=self._status[command_id],
            history=tuple(self._history[command_id]),
        )

    def ack(self, command_id: str) -> None:
        """Mark a command delivered/executed. Idempotent; unknown ids are ignored."""
        self._acked.add(command_id)

    def transition(
        self,
        command_id: str,
        to_status: CommandStatus,
        at: datetime | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Move a command to ``to_status``.

        Raises ``ValueError`` if the transition is not allowed by
        :func:`commands.status.is_valid_transition` (the binding state
        machine). Raises ``KeyError`` if the command is unknown.

        The transition is recorded in the command's history. The
        dispatcher's state is updated atomically: a failure leaves
        the previous status in place.
        """
        if command_id not in self._status:
            raise KeyError(f"unknown command {command_id!r}")
        frm = self._status[command_id]
        if not is_valid_transition(frm, to_status):
            raise ValueError(
                f"invalid transition {frm.value} -> {to_status.value} "
                f"for command {command_id!r}"
            )
        when = at or self._clock()
        self._status[command_id] = to_status
        self._history[command_id].append(
            StatusTransition(
                from_status=frm,
                to_status=to_status,
                at=when,
                detail=detail or {},
            )
        )

    def get_status(self, command_id: str) -> CommandStatus:
        """Return the current lifecycle status. Raises KeyError if unknown."""
        if command_id not in self._status:
            raise KeyError(f"unknown command {command_id!r}")
        return self._status[command_id]

    def get_history(self, command_id: str) -> list[StatusTransition]:
        """Return the ordered list of lifecycle transitions.

        The first entry is the initial PENDING at issue time. Each
        subsequent entry is one ``transition()`` call. The list is
        append-only: ``transition()`` never edits prior entries.

        Raises KeyError if unknown.
        """
        if command_id not in self._history:
            raise KeyError(f"unknown command {command_id!r}")
        return list(self._history[command_id])

    def pending(self) -> list[SignedCommand]:
        """Commands still needing delivery: unacked and unexpired, in issue order."""
        now = self._clock()
        return [
            signed
            for command_id, signed in self._signed.items()
            if command_id not in self._acked and not signed.command.is_expired(now)
        ]
