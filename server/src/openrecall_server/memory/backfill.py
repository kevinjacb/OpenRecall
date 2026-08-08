"""One-shot startup backfill of atom conversation time (spec §1.1).

Atoms written before ``occurred_at`` existed carry only ``created_at`` — the
moment the *server* extracted them. That is fine for ordering within one
batch and wrong across batches, so the Memories tab would show a plausible
but false timeline for all historical memories.

The source capture event knows the real answer: its ``created_at`` is the
wall clock at capture. This sweep joins atoms to events by
``source_event_id`` and fills the gap.

It is deliberately *not* a SQL migration: atoms and events live in separate
SQLite files, so the join has to happen in Python anyway, and doing it here
keeps it idempotent (rows with a value are never touched), interruptible,
and applicable to the in-memory store the tests use.
"""
from __future__ import annotations

import logging

from ..events.store import EventStore
from .store import AtomStore

log = logging.getLogger(__name__)


def backfill_occurred_at(events: EventStore, atoms: AtomStore) -> int:
    """Fill ``occurred_at`` on atoms that lack it. Returns rows updated.

    Atoms whose source event is gone are left alone — ``timeline_at`` falls
    back to ``created_at`` for them, which is a documented approximation
    rather than a fabricated timestamp.

    Best-effort: a failure here must never stop the server from starting.
    The fallback is only a cosmetic ordering error on historical rows.
    """
    try:
        occurred_by_event = {
            event.event_id: event.created_at
            for session_id in events.sessions()
            for event in events.events(session_id)
        }
        if not occurred_by_event:
            return 0
        updated = atoms.backfill_occurred_at(occurred_by_event)
        if updated:
            log.info("occurred_at_backfill rows=%d", updated)
        return updated
    except Exception:
        log.exception("occurred_at_backfill_failed")
        return 0
