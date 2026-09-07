"""Per-request scope for MCP tool calls.

A bearer header proves *who* is calling; it cannot prove *what for*. Every tool
takes a request_id that must name an open ledger entry, so the server knows the
trigger kind, the session, and which atoms have been returned so far.

Two things depend on this:
  - the proactive ISSUE_COMMAND prohibition (planner.py:231) survives the move
    out of process, because trigger_kind lives on the entry;
  - provenance becomes enforceable — Phase 2's agent.respond accepts only atom
    ids that this request actually retrieved.

In-memory and process-local by design: a ledger entry is meaningful only while
the request that opened it is running.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Iterable, Literal

TriggerKind = Literal["user_request", "proactive"]

DEFAULT_TTL_S = 120


class LedgerClosedError(Exception):
    """Raised when a tool call names a request that is closed or expired."""


class ResponseAlreadyRecordedError(Exception):
    """Raised when a request tries to answer twice.

    One request, one answer: a second call would let an agent overwrite a
    provenance-checked answer with an unchecked one.
    """


@dataclass(frozen=True)
class AgentResponse:
    """The structured answer an agent produced for one request.

    This — not the transport's stdout — is what a planner reads to build its
    result, because only this passed the citation gate.
    """

    kind: str
    text: str
    atom_ids: tuple[str, ...]
    confidence: float | None
    command_id: str | None
    memory_atom_id: str | None
    reminder_id: str | None


@dataclass
class LedgerEntry:
    request_id: str
    session_id: str | None
    trigger_kind: TriggerKind
    opened_at: datetime
    deadline: datetime
    atoms: set[str] = field(default_factory=set)
    response: AgentResponse | None = None


class RequestLedger:
    """Thread-safe registry of open per-request scopes.

    Guarded by ``self._lock``, matching the store pattern in
    ``memory/store.py``: the gateway and HTTP layer run on different threads,
    and MCP tool calls (open/get/record_atoms/close) can arrive from either.
    """

    def __init__(self, clock) -> None:
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[str, LedgerEntry] = {}

    def open(self, request_id: str, *, session_id: str | None,
             trigger_kind: TriggerKind,
             ttl_s: int = DEFAULT_TTL_S) -> LedgerEntry:
        now = self._clock.now()
        entry = LedgerEntry(
            request_id=request_id,
            session_id=session_id,
            trigger_kind=trigger_kind,
            opened_at=now,
            deadline=now + timedelta(seconds=ttl_s),
        )
        with self._lock:
            self._entries[request_id] = entry
        return entry

    def get(self, request_id: str) -> LedgerEntry | None:
        with self._lock:
            return self._get_live_locked(request_id)

    def close(self, request_id: str) -> None:
        with self._lock:
            self._entries.pop(request_id, None)

    def record_atoms(self, request_id: str, atom_ids: Iterable[str]) -> None:
        # Look up and mutate under a single lock acquisition rather than
        # calling self.get() and then re-locking: two acquisitions would open
        # a check-then-act window where a concurrent close() could evict the
        # entry between the check and the mutation, silently writing atoms
        # into a dict-detached LedgerEntry that cited() can never see again.
        # That specific failure is fail-safe (a lost citation makes Phase 2's
        # provenance check *more* conservative, never less), but it is free
        # to close the window outright by not releasing the lock in between,
        # so we do that instead of accepting the race.
        with self._lock:
            entry = self._get_live_locked(request_id)
            if entry is None:
                raise LedgerClosedError(f"request not open: {request_id}")
            entry.atoms.update(atom_ids)

    def record_response(self, request_id: str,
                        response: AgentResponse) -> None:
        with self._lock:
            entry = self._get_live_locked(request_id)
            if entry is None:
                raise LedgerClosedError(f"request not open: {request_id}")
            if entry.response is not None:
                raise ResponseAlreadyRecordedError(
                    f"request already answered: {request_id}")
            entry.response = response

    def response(self, request_id: str) -> AgentResponse | None:
        with self._lock:
            entry = self._get_live_locked(request_id)
            return entry.response if entry else None

    def cited(self, request_id: str) -> frozenset[str]:
        entry = self.get(request_id)
        return frozenset(entry.atoms) if entry else frozenset()

    def may_issue_command(self, request_id: str) -> bool:
        entry = self.get(request_id)
        return entry is not None and entry.trigger_kind == "user_request"

    def _get_live_locked(self, request_id: str) -> LedgerEntry | None:
        """Return the entry for ``request_id`` if present and unexpired,
        evicting it first if its deadline has passed. Caller must hold
        ``self._lock``."""
        entry = self._entries.get(request_id)
        if entry is None:
            return None
        if self._clock.now() >= entry.deadline:
            del self._entries[request_id]
            return None
        return entry
