"""Phase 1 — list mode, stats, and conversation time (spec §1.1–§1.3).

Both backends run the same suite via the ``store`` fixture: the in-memory
store is what the tests use everywhere else, so any divergence from SQLite
would mean the tests pass and production doesn't.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.store import InMemoryAtomStore, SqliteAtomStore

_NOW = datetime(2026, 8, 8, 12, 0, 0, tzinfo=timezone.utc)


def atom(
    atom_id: str,
    *,
    session_id: str = "s1",
    kind: str = "fact",
    occurred_at: datetime | None = None,
    created_at: datetime = _NOW,
) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"{session_id}:{atom_id}",
        kind=kind,
        text=f"memory {atom_id}",
        created_at=created_at,
        occurred_at=occurred_at,
        start_ms=0,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryAtomStore()
    return SqliteAtomStore(tmp_path / "atoms.db")


# ---- §1.1 conversation time -------------------------------------------------


def test_occurred_at_round_trips(store):
    when = _NOW - timedelta(days=3)
    store.append(atom("a1", occurred_at=when))

    (got,) = store.atoms("s1")

    assert got.occurred_at == when
    assert got.timeline_at == when


def test_timeline_at_falls_back_to_created_at(store):
    """A row written before `occurred_at` existed must still have a place on
    the timeline rather than sorting at NULL."""
    store.append(atom("a1", occurred_at=None))

    (got,) = store.atoms("s1")

    assert got.occurred_at is None
    assert got.timeline_at == got.created_at


def test_backfill_sets_occurred_at_by_source_event(store):
    when = _NOW - timedelta(days=2)
    store.append(atom("a1", occurred_at=None))

    updated = store.backfill_occurred_at({"s1:a1": when})

    assert updated == 1
    assert store.atoms("s1")[0].occurred_at == when


def test_backfill_never_overwrites_a_known_value(store):
    real = _NOW - timedelta(days=2)
    store.append(atom("a1", occurred_at=real))

    updated = store.backfill_occurred_at({"s1:a1": _NOW})

    assert updated == 0
    assert store.atoms("s1")[0].occurred_at == real


def test_backfill_skips_atoms_with_no_matching_event(store):
    """Spec §1.1: an atom whose session is gone keeps the created_at fallback
    rather than being dropped or guessed at."""
    store.append(atom("a1", occurred_at=None))

    assert store.backfill_occurred_at({"other:99": _NOW}) == 0
    assert store.atoms("s1")[0].occurred_at is None


# ---- §1.2 list mode ---------------------------------------------------------


def test_list_orders_by_conversation_time_not_extraction_time(store):
    """D6, stated as a test: a batch extracted in one pass shares one
    `created_at`, so ordering on it would be arbitrary."""
    store.append(atom("a1", occurred_at=_NOW - timedelta(hours=3)))
    store.append(atom("a2", occurred_at=_NOW - timedelta(hours=1)))
    store.append(atom("a3", occurred_at=_NOW - timedelta(hours=2)))

    atoms, _ = store.list()

    assert [a.atom_id for a in atoms] == ["a2", "a3", "a1"]


def test_list_filters_by_kind_exactly(store):
    store.append(atom("a1", kind="task"))
    store.append(atom("a2", kind="fact"))

    atoms, _ = store.list(kind="task")

    assert [a.atom_id for a in atoms] == ["a1"]


def test_list_unknown_kind_returns_an_empty_page_not_an_error(store):
    """The extractor's kind vocabulary is free-form (spec §1.2), so an
    unrecognised kind is a legitimate query with no results."""
    store.append(atom("a1", kind="fact"))

    atoms, cursor = store.list(kind="Decisions")

    assert atoms == []
    assert cursor is None


def test_list_filters_by_session(store):
    store.append(atom("a1", session_id="s1"))
    store.append(atom("a2", session_id="s2"))

    atoms, _ = store.list(session_id="s2")

    assert [a.atom_id for a in atoms] == ["a2"]


def test_list_pages_through_every_atom_exactly_once(store):
    for i in range(5):
        store.append(atom(f"a{i}", occurred_at=_NOW - timedelta(hours=i)))

    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = store.list(limit=2, before=cursor)
        seen.extend(a.atom_id for a in page)
        if cursor is None:
            break

    assert seen == ["a0", "a1", "a2", "a3", "a4"]


def test_list_final_page_returns_no_cursor(store):
    store.append(atom("a1"))

    _page, cursor = store.list(limit=10)

    assert cursor is None


def test_list_breaks_ties_on_atom_id(store):
    """Atoms extracted from one window share a timestamp; without the id
    tiebreak the keyset anchor is ambiguous and paging can loop or skip."""
    same = _NOW - timedelta(hours=1)
    for i in range(4):
        store.append(atom(f"a{i}", occurred_at=same))

    seen: list[str] = []
    cursor = None
    while True:
        page, cursor = store.list(limit=2, before=cursor)
        seen.extend(a.atom_id for a in page)
        if cursor is None:
            break

    assert seen == ["a3", "a2", "a1", "a0"]


def test_list_rejects_a_non_positive_limit(store):
    with pytest.raises(ValueError):
        store.list(limit=0)


# ---- §1.3 stats -------------------------------------------------------------


def test_stats_counts_total_and_by_kind(store):
    store.append(atom("a1", kind="task"))
    store.append(atom("a2", kind="task"))
    store.append(atom("a3", kind="fact"))

    stats = store.stats(now=_NOW)

    assert stats.total == 3
    assert stats.by_kind == {"task": 2, "fact": 1}


def test_stats_added_24h_uses_conversation_time(store):
    """The header reads "6 added today". An overnight batch extracting last
    week's sessions must not report a week of memories as today's."""
    store.append(atom("old", occurred_at=_NOW - timedelta(days=7), created_at=_NOW))
    store.append(atom("new", occurred_at=_NOW - timedelta(hours=2), created_at=_NOW))

    stats = store.stats(now=_NOW)

    assert stats.total == 2
    assert stats.added_24h == 1


def test_stats_on_an_empty_store(store):
    stats = store.stats(now=_NOW)

    assert stats.total == 0
    assert stats.added_24h == 0
    assert stats.by_kind == {}
