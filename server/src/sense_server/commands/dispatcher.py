"""Command dispatcher: at-least-once delivery with idempotent acks and expiry.

Signs and tracks outstanding commands. :meth:`pending` returns the commands still
needing delivery (issued, not yet acked, not yet expired) so a transport can
re-offer them until they land — the wearable executes each ``command_id`` at most
once, so re-delivery is safe. Acks are idempotent, and issuing a duplicate
``command_id`` is a no-op.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable

from .model import Command
from .signing import CommandSigner, SignedCommand

Clock = Callable[[], datetime]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class CommandDispatcher:
    def __init__(self, signer: CommandSigner, clock: Clock = _utcnow) -> None:
        self._signer = signer
        self._clock = clock
        self._signed: dict[str, SignedCommand] = {}  # command_id -> signed (issue order)
        self._acked: set[str] = set()

    def issue(self, command: Command) -> SignedCommand:
        """Sign and track a command; idempotent by command_id."""
        existing = self._signed.get(command.command_id)
        if existing is not None:
            return existing
        signed = self._signer.sign(command)
        self._signed[command.command_id] = signed
        return signed

    def ack(self, command_id: str) -> None:
        """Mark a command delivered/executed. Idempotent; unknown ids are ignored."""
        self._acked.add(command_id)

    def pending(self) -> list[SignedCommand]:
        """Commands still needing delivery: unacked and unexpired, in issue order."""
        now = self._clock()
        return [
            signed
            for command_id, signed in self._signed.items()
            if command_id not in self._acked and not signed.command.is_expired(now)
        ]
