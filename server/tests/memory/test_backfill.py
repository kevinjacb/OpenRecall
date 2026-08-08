"""Startup backfill of atom conversation time (spec §1.1)."""
from __future__ import annotations

from datetime import datetime, timezone

from opensapien_server.events.model import CaptureEvent
from opensapien_server.events.store import InMemoryEventStore
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.backfill import backfill_occurred_at
from opensapien_server.memory.store import InMemoryAtomStore

_SPOKEN = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
_EXTRACTED = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


def _event(event_id: str, session_id: str = "s1") -> CaptureEvent:
    return CaptureEvent(
        event_id=event_id,
        session_id=session_id,
        seq=0,
        kind="transcript",
        created_at=_SPOKEN,
        text="hello",
        duration_ms=1000,
        start_ms=0,
    )


def _atom(atom_id: str, source_event_id: str) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id=source_event_id,
        kind="fact",
        text="a fact",
        created_at=_EXTRACTED,
        start_ms=0,
    )


def test_backfill_dates_a_legacy_atom_from_its_source_event():
    events = InMemoryEventStore()
    events.append(_event("s1:0"))
    atoms = InMemoryAtomStore()
    atoms.append(_atom("a1", "s1:0"))

    assert backfill_occurred_at(events, atoms) == 1

    assert atoms.atoms("s1")[0].timeline_at == _SPOKEN


def test_backfill_leaves_an_orphan_atom_on_its_created_at_fallback():
    """An atom whose session was deleted keeps a documented approximation
    rather than a fabricated timestamp."""
    atoms = InMemoryAtomStore()
    atoms.append(_atom("a1", "gone:0"))

    assert backfill_occurred_at(InMemoryEventStore(), atoms) == 0

    got = atoms.atoms("s1")[0]
    assert got.occurred_at is None
    assert got.timeline_at == _EXTRACTED


def test_backfill_is_idempotent():
    events = InMemoryEventStore()
    events.append(_event("s1:0"))
    atoms = InMemoryAtomStore()
    atoms.append(_atom("a1", "s1:0"))

    assert backfill_occurred_at(events, atoms) == 1
    assert backfill_occurred_at(events, atoms) == 0


def test_backfill_on_an_empty_store_is_a_noop():
    assert backfill_occurred_at(InMemoryEventStore(), InMemoryAtomStore()) == 0
