"""Canonical id-generator seam for the server.

Every fresh identifier (request_id, retrieval_trace_id, audit_id) flows
through a single :class:`IdGenerator`. Production uses
:class:`UuidIdGenerator`; tests inject :class:`DeterministicIdGenerator`
so logs and audit records are reproducible.
"""
from __future__ import annotations

import uuid
from typing import Protocol, runtime_checkable


@runtime_checkable
class IdGenerator(Protocol):
    """Single seam for "give me a fresh id" — the server never calls
    :func:`uuid.uuid4` directly."""

    def new(self) -> str:
        """Return a fresh, globally-unique identifier as a string."""
        ...


class UuidIdGenerator:
    """Production generator — delegates to :mod:`uuid` (UUIDv4)."""

    def new(self) -> str:
        return str(uuid.uuid4())


class DeterministicIdGenerator:
    """Test generator — returns ``"{prefix}-{counter:0Nd}"`` with
    incrementing counter.

    Two :class:`DeterministicIdGenerator` instances are independent; counter
    state is per-instance. A non-default ``prefix`` lets multiple id
    namespaces (request_id, trace_id, audit_id) coexist in one test.
    """

    def __init__(self, prefix: str = "trace", pad: int = 4) -> None:
        self._prefix = prefix
        self._pad = pad
        self._counter = 0

    def new(self) -> str:
        self._counter += 1
        return f"{self._prefix}-{self._counter:0{self._pad}d}"
