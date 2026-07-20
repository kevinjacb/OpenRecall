"""Tests for the memory-atom store (§G) and its extraction cursor.

Memory atoms are the *derived* layer: structured memories extracted from capture
events. The store is idempotent by ``atom_id`` and tracks a per-session extraction
**cursor** (the seq of the last capture event turned into atoms) so extraction is
resumable and exactly-once even when an event yields zero atoms.

One suite runs against both backends (in-memory + sqlite) via the ``store`` fixture.
"""

from datetime import datetime, timezone

import pytest

from sense_server.memory.atom import MemoryAtom
from sense_server.memory.store import InMemoryAtomStore, SqliteAtomStore


def atom(session_id: str, event_seq: int, idx: int, kind: str = "fact") -> MemoryAtom:
    return MemoryAtom(
        atom_id=f"{session_id}:{event_seq}:{idx}",
        session_id=session_id,
        source_event_id=f"{session_id}:{event_seq}",
        kind=kind,
        text=f"memory {event_seq}/{idx}",
        created_at=datetime(2026, 6, 30, 12, 0, 0, tzinfo=timezone.utc),
        start_ms=event_seq * 5000,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryAtomStore()
    return SqliteAtomStore(tmp_path / "atoms.db")


def test_append_new_atom_is_stored_and_readable(store):
    assert store.append(atom("s1", 0, 0, kind="task")) is True

    atoms = store.atoms("s1")
    assert len(atoms) == 1
    assert atoms[0].kind == "task"
    assert atoms[0].source_event_id == "s1:0"


def test_append_is_idempotent_by_atom_id(store):
    assert store.append(atom("s1", 0, 0)) is True
    assert store.append(atom("s1", 0, 0)) is False

    assert len(store.atoms("s1")) == 1


def test_atoms_are_scoped_per_session_in_start_order(store):
    store.append(atom("s1", 1, 0))
    store.append(atom("s1", 0, 0))
    store.append(atom("s2", 0, 0))

    assert [a.start_ms for a in store.atoms("s1")] == [0, 5000]
    assert [a.source_event_id for a in store.atoms("s2")] == ["s2:0"]


def test_has_reports_membership_by_atom_id(store):
    assert store.has("s1:0:0") is False
    store.append(atom("s1", 0, 0))
    assert store.has("s1:0:0") is True


def test_extraction_cursor_defaults_to_minus_one_and_advances(store):
    assert store.get_cursor("s1") == -1  # nothing extracted yet

    store.set_cursor("s1", 3)
    assert store.get_cursor("s1") == 3
    assert store.get_cursor("s2") == -1  # independent per session


def test_sqlite_atoms_and_cursor_persist_across_reopen(tmp_path):
    path = tmp_path / "atoms.db"
    s1 = SqliteAtomStore(path)
    s1.append(atom("s1", 0, 0, kind="fact"))
    s1.set_cursor("s1", 0)

    s2 = SqliteAtomStore(path)
    assert len(s2.atoms("s1")) == 1
    assert s2.get_cursor("s1") == 0


def test_sqlite_atom_store_is_usable_from_another_thread(tmp_path):
    """The extraction worker calls `atom_store.append` from a thread
    pool (`asyncio.to_thread` in ``ExtractionWorker._run`` and the
    reconcile sweep in ``start()``). The atom store's sqlite
    connection must be safe to use from a thread that did not create
    it — otherwise the entire production extraction path raises
    ``ProgrammingError: SQLite objects created in a thread can only
    be used in that same thread`` and silently fails (the
    ``process_session`` ``try/except`` increments
    ``INDEXING_FAILURES_TOTAL`` and the cursor never advances).

    Without this test, the in-memory + sqlite divergence is silent:
    in-memory extraction always works, sqlite extraction never
    writes an atom. Mirrors ``test_sqlite_store_is_usable_from_another_thread``
    in ``tests/events/test_store.py`` for the event store.
    """
    import threading

    store = SqliteAtomStore(tmp_path / "atoms.db")
    errors: list[Exception] = []

    def worker() -> None:
        try:
            store.append(atom("s1", 0, 0, kind="from-thread"))
            store.set_cursor("s1", 0)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    thread = threading.Thread(target=worker)
    thread.start()
    thread.join()

    assert errors == [], f"thread-affinity errors: {errors}"
    assert [a.text for a in store.atoms("s1")] == ["memory 0/0"]
    assert store.get_cursor("s1") == 0


def test_sqlite_atom_store_handles_concurrent_writes(tmp_path):
    """Two threads appending different atoms simultaneously must both
    succeed and both be visible after a join. With the
    ``check_same_thread=False`` + lock pattern used by
    :class:`SqliteEventStore`, sqlite serialises the writes."""
    import threading

    store = SqliteAtomStore(tmp_path / "atoms.db")
    errors: list[Exception] = []
    barrier = threading.Barrier(2)

    def worker(idx: int) -> None:
        try:
            barrier.wait(timeout=2.0)
            store.append(atom("s1", 0, idx, kind=f"k{idx}"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == [], f"concurrent write errors: {errors}"
    assert len(store.atoms("s1")) == 2
