"""Tests for the memory-atom store (§G) and its extraction cursor.

Memory atoms are the *derived* layer: structured memories extracted from capture
events. The store is idempotent by ``atom_id`` and tracks a per-session extraction
**cursor** (the seq of the last capture event turned into atoms) so extraction is
resumable and exactly-once even when an event yields zero atoms.

One suite runs against both backends (in-memory + sqlite) via the ``store`` fixture.
"""

from datetime import datetime, timezone

import pytest

from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore, SqliteAtomStore


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


# --- version-aware extraction cursor ---------------------------------------
#
# The cursor records "the last capture event seq already extracted" so the
# extraction pass is resumable. But the cursor is only meaningful relative to
# the extractor that produced it: if the extraction algorithm or prompt
# changes, a cursor stamped with the old version must be treated as stale
# (i.e. "unextracted") so the new extractor re-processes those events.
#
# This is the structural fix for the live incident where the per-event
# extractor advanced every session's cursor to max while producing ~0 atoms;
# the windowed extractor that replaced it then saw `pending` empty for every
# historical session and silently skipped them — transcripts piled up, no
# memories ever formed. A versioned cursor makes the next algorithm change
# invalidate the old cursors automatically instead of locking the data out.


def test_get_cursor_returns_last_seq_when_version_matches(store):
    store.set_cursor("s1", 5, extractor_version="v1")
    assert store.get_cursor("s1", extractor_version="v1") == 5


def test_get_cursor_treats_mismatched_version_as_unextracted(store):
    store.set_cursor("s1", 5, extractor_version="v1")
    # a different extractor version must see the cursor as stale (-1)
    assert store.get_cursor("s1", extractor_version="v2") == -1


def test_set_cursor_overwrites_with_new_version(store):
    store.set_cursor("s1", 5, extractor_version="v1")
    store.set_cursor("s1", 7, extractor_version="v2")
    # the v1 cursor is gone; only the v2 stamp remains
    assert store.get_cursor("s1", extractor_version="v1") == -1
    assert store.get_cursor("s1", extractor_version="v2") == 7


def test_set_cursor_without_version_records_legacy(store):
    """A cursor written with no version (the legacy / back-compat path,
    e.g. the old per-event ExtractionPipeline) is stamped 'legacy' and must
    be treated as stale by any versioned extractor — so a new extractor
    re-processes the session instead of skipping it."""
    store.set_cursor("s1", 9)  # no version → legacy
    assert store.get_cursor("s1", extractor_version="v1") == -1
    # but an unversioned read (back-compat) still sees the seq
    assert store.get_cursor("s1") == 9


def test_get_cursor_without_version_arg_is_backwards_compatible(store):
    """Callers that don't pass a version get the raw last_seq regardless of
    the recorded version — preserving the original contract for the many
    existing call sites (tests, the old pipeline) that don't track versions."""
    store.set_cursor("s1", 5, extractor_version="v1")
    assert store.get_cursor("s1") == 5
    store.set_cursor("s1", 6, extractor_version="v2")
    assert store.get_cursor("s1") == 6


def test_sqlite_legacy_cursor_row_is_treated_as_stale(tmp_path):
    """A pre-versioning database has extraction_cursor rows with no version
    column. The migration backfills 'legacy'; a versioned extractor must
    re-extract those sessions rather than skip them."""
    store = SqliteAtomStore(tmp_path / "atoms.db")
    store.set_cursor("s1", 12)  # legacy stamp
    assert store.get_cursor("s1", extractor_version="v1") == -1
    # and a fresh versioned write supersedes the legacy row
    store.set_cursor("s1", 12, extractor_version="v1")
    assert store.get_cursor("s1", extractor_version="v1") == 12


def test_store_round_trips_speaker_columns(store):
    a = MemoryAtom(
        atom_id="s1:0:0", session_id="s1", source_event_id="s1:0", kind="fact",
        text="x", created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), start_ms=0,
        speaker="uuid-1", speaker_confidence=0.82, speaker_assignment="confirmed",
    )
    assert store.append(a) is True
    out = store.atoms("s1")[0]
    assert out.speaker == "uuid-1"
    assert out.speaker_confidence == 0.82
    assert out.speaker_assignment == "confirmed"


def test_store_round_trips_null_speaker(store):
    a = MemoryAtom(
        atom_id="s1:1:0", session_id="s1", source_event_id="s1:1", kind="fact",
        text="x", created_at=datetime(2026, 7, 26, tzinfo=timezone.utc), start_ms=1000,
    )
    store.append(a)
    out = store.atoms("s1")[0]
    assert out.speaker is None
    assert out.speaker_assignment is None
