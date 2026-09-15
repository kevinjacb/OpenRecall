"""Startup backfill of atom conversation time (spec §1.1)."""
from __future__ import annotations

from datetime import datetime, timezone

from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.backfill import backfill_index_occurred_at, backfill_occurred_at
from openrecall_server.memory.index import SqliteMemoryIndex
from openrecall_server.memory.store import InMemoryAtomStore

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


# --- SqliteMemoryIndex.occurred_at backfill (task-1, D6 production fix) ---

_FILLED_AT = datetime(2026, 8, 1, 9, 0, 0, tzinfo=timezone.utc)
_GAP_AT = datetime(2026, 8, 2, 10, 0, 0, tzinfo=timezone.utc)
_WRONG_AT = datetime(2099, 1, 1, tzinfo=timezone.utc)  # must never land in the index


def _memory_atom(atom_id: str, occurred_at: datetime | None) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text="a fact",
        created_at=_EXTRACTED,
        start_ms=0,
        occurred_at=occurred_at,
    )


class _RaisingAtomStore:
    """Stand-in whose iter_atoms() always raises — proves the best-effort
    contract: backfill_index_occurred_at must never propagate this."""

    def iter_atoms(self):
        raise RuntimeError("boom: atom store unavailable")


def test_backfill_index_occurred_at_fills_gap_and_leaves_existing_value(tmp_path):
    index = SqliteMemoryIndex(tmp_path / "index.db")
    # Index already has a_filled with a real occurred_at, and a_gap with none.
    index.add(_memory_atom("a_filled", _FILLED_AT), [1.0, 0.0])
    index.add(_memory_atom("a_gap", None), [0.0, 1.0])

    # The atom store holds an occurred_at for BOTH atom ids — a_filled's
    # value here deliberately disagrees with what's already in the index,
    # so overwriting it (a bug) is distinguishable from leaving it alone.
    atoms = InMemoryAtomStore()
    atoms.append(_memory_atom("a_filled", _WRONG_AT))
    atoms.append(_memory_atom("a_gap", _GAP_AT))

    updated = backfill_index_occurred_at(atoms, index)
    assert updated == 1

    [filled] = index.search("s1", [1.0, 0.0], k=1)
    [gap] = index.search("s1", [0.0, 1.0], k=1)
    assert filled.atom.occurred_at == _FILLED_AT  # unchanged, not overwritten
    assert gap.atom.occurred_at == _GAP_AT  # filled


def test_backfill_index_occurred_at_is_best_effort_on_raising_store(tmp_path):
    index = SqliteMemoryIndex(tmp_path / "index.db")
    index.add(_memory_atom("a_gap", None), [0.0, 1.0])

    assert backfill_index_occurred_at(_RaisingAtomStore(), index) == 0
    # Nothing was touched.
    [gap] = index.search("s1", [0.0, 1.0], k=1)
    assert gap.atom.occurred_at is None
