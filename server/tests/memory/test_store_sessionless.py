from datetime import datetime, timezone

from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.store import InMemoryAtomStore, SqliteAtomStore


def _sceneless(digest, when):
    return MemoryAtom(
        atom_id=f"scene:{digest}", session_id=None,
        source_event_id=f"blob:{digest}", kind="scene", text="a scene",
        created_at=when, source_pipeline_version="vision",
        occurred_at=when, start_ms=0,
    )


def test_sqlite_store_persists_sessionless_atom_round_trip(tmp_path):
    when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    store = SqliteAtomStore(tmp_path / "atoms.db")
    assert store.append(_sceneless("d1", when)) is True
    # read back via iter_atoms
    atoms = [a for a in store.iter_atoms() if a.atom_id == "scene:d1"]
    assert len(atoms) == 1
    assert atoms[0].session_id is None          # decoded back from ""
    # sessions() must NOT include the sessionless sentinel
    assert store.sessions() == []


def test_sqlite_store_sessionless_and_sessioned_coexist(tmp_path):
    when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    store = SqliteAtomStore(tmp_path / "atoms.db")
    store.append(_sceneless("d1", when))
    store.append(MemoryAtom(
        atom_id="s1:scene:d2", session_id="s1", source_event_id="blob:d2",
        kind="scene", text="x", created_at=when, source_pipeline_version="vision",
        occurred_at=when, start_ms=0,
    ))
    assert sorted(store.sessions()) == ["s1"]    # sessionless excluded
    # idempotent re-append of the sessionless atom
    assert store.append(_sceneless("d1", when)) is False


def test_inmemory_store_sessions_excludes_none():
    when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    store = InMemoryAtomStore()
    store.append(_sceneless("d1", when))
    store.append(MemoryAtom(
        atom_id="s1:scene:d2", session_id="s1", source_event_id="blob:d2",
        kind="scene", text="x", created_at=when, source_pipeline_version="vision",
        occurred_at=when, start_ms=0,
    ))
    assert store.sessions() == ["s1"]            # None excluded
    assert [a for a in store.iter_atoms() if a.session_id is None][0].atom_id == "scene:d1"


def test_sqlite_store_atoms_none_retrieves_sessionless(tmp_path):
    # The _SESSIONLESS="" sentinel round-trips None↔"" at append/_row_to_atom;
    # atoms(None) must map None→"" at the query boundary too, else
    # `WHERE session_id = NULL` matches nothing (the sentinel contract).
    when = datetime(2026, 8, 22, tzinfo=timezone.utc)
    store = SqliteAtomStore(tmp_path / "atoms.db")
    store.append(_sceneless("d1", when))
    store.append(MemoryAtom(
        atom_id="s1:scene:d2", session_id="s1", source_event_id="blob:d2",
        kind="scene", text="x", created_at=when, source_pipeline_version="vision",
        occurred_at=when, start_ms=0,
    ))
    # atoms(None) returns ONLY the sessionless atom, decoded back to None.
    none_atoms = store.atoms(None)
    assert len(none_atoms) == 1
    assert none_atoms[0].session_id is None
    assert none_atoms[0].atom_id == "scene:d1"
    # atoms("s1") still returns only the sessioned atom (no sentinel leak).
    assert [a.atom_id for a in store.atoms("s1")] == ["s1:scene:d2"]