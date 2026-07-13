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
        # command_id -> signed (issue order)
        self._signed: dict[str, SignedCommand] = {}
        # idempotency_key -> command_id (for dedup across retries)
        self._idem: dict[str, str] = {}
        self._acked: set[str] = set()

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
