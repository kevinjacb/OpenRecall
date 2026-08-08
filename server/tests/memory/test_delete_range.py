"""Phase 5 — the store-level cascade primitives (spec §5.2)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.index import InMemoryMemoryIndex, SqliteMemoryIndex
from openrecall_server.memory.store import InMemoryAtomStore, SqliteAtomStore

_NOW = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc)


def _atom(atom_id, *, session_id="s1", start_ms=0):
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"{session_id}:0",
        kind="fact",
        text=f"memory {atom_id}",
        created_at=_NOW,
        occurred_at=_NOW,
        start_ms=start_ms,
    )


@pytest.fixture(params=["memory", "sqlite"])
def store(request, tmp_path):
    if request.param == "memory":
        return InMemoryAtomStore()
    return SqliteAtomStore(tmp_path / "atoms.db")


@pytest.fixture(params=["memory", "sqlite"])
def index(request, tmp_path):
    if request.param == "memory":
        return InMemoryMemoryIndex()
    return SqliteMemoryIndex(tmp_path / "index.db")


# ---- AtomStore.delete_range -------------------------------------------------


def test_delete_range_removes_only_the_window(store):
    store.append(_atom("a1", start_ms=0))
    store.append(_atom("a2", start_ms=500))
    store.append(_atom("a3", start_ms=2000))

    deleted = store.delete_range("s1", 0, 1000)

    assert sorted(deleted) == ["a1", "a2"]
    assert [a.atom_id for a in store.atoms("s1")] == ["a3"]


def test_delete_range_is_end_exclusive(store):
    store.append(_atom("a1", start_ms=1000))

    assert store.delete_range("s1", 0, 1000) == []
    assert len(store.atoms("s1")) == 1


def test_delete_range_leaves_other_sessions_alone(store):
    store.append(_atom("a1", session_id="s1"))
    store.append(_atom("a2", session_id="s2"))

    store.delete_range("s1", 0, 1000)

    assert [a.atom_id for a in store.atoms("s2")] == ["a2"]


def test_delete_range_returns_ids_for_the_vector_cascade(store):
    """The caller needs these to drop the vectors — an orphaned vector keeps
    surfacing a deleted memory in search."""
    store.append(_atom("a1", start_ms=0))

    assert store.delete_range("s1", 0, 1000) == ["a1"]


def test_delete_range_on_an_empty_window_is_a_noop(store):
    assert store.delete_range("s1", 0, 1000) == []


def test_a_deleted_atom_id_can_be_reused(store):
    store.append(_atom("a1", start_ms=0))
    store.delete_range("s1", 0, 1000)

    assert store.append(_atom("a1", start_ms=0)) is True


def test_deleted_atoms_drop_out_of_stats_and_list(store):
    store.append(_atom("a1", start_ms=0))
    store.append(_atom("a2", start_ms=2000))

    store.delete_range("s1", 0, 1000)

    assert store.stats(now=_NOW).total == 1
    assert [a.atom_id for a in store.list()[0]] == ["a2"]


# ---- MemoryIndex.delete_atoms -----------------------------------------------


def test_delete_atoms_removes_the_vectors(index):
    index.add(_atom("a1"), [0.1, 0.2])
    index.add(_atom("a2"), [0.3, 0.4])

    assert index.delete_atoms(["a1"]) == 1

    assert index.has("a1") is False
    assert index.has("a2") is True


def test_delete_atoms_drops_them_from_search(index):
    """The point of the cascade: a deleted memory must stop being findable."""
    index.add(_atom("a1"), [1.0, 0.0])
    index.add(_atom("a2"), [1.0, 0.0])

    index.delete_atoms(["a1"])

    assert [r.atom.atom_id for r in index.search("s1", [1.0, 0.0], 10)] == ["a2"]


def test_delete_atoms_ignores_unknown_ids(index):
    index.add(_atom("a1"), [0.1, 0.2])

    assert index.delete_atoms(["nope"]) == 0
    assert index.has("a1") is True


def test_delete_atoms_of_an_empty_list_is_a_noop(index):
    assert index.delete_atoms([]) == 0
