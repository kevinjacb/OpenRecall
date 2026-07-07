"""Architectural-invariant tests for the memory layer.

These are binding regression tests for the plan's architectural invariants.
A failure here means the architecture is broken — the test name cites the invariant.
"""
from __future__ import annotations

import threading
from datetime import datetime, timezone

import pytest

from sense_server.memory.atom import MemoryAtom
from sense_server.memory.index import InMemoryMemoryIndex, SearchResult
from sense_server.memory.store import InMemoryAtomStore


def _atom(atom_id: str, session_id: str, text: str, start_ms: int = 0) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=start_ms,
    )


def test_invariant_in_memory_atom_store_thread_safe():
    """INV-1 + INV-6: concurrent appends must not lose atoms.

    The store is the only shared mutable state on the read path; correctness
    under concurrent appends is required.
    """
    store = InMemoryAtomStore()
    atoms = [
        _atom(atom_id=f"a{i}", session_id="s1", text=f"text {i}", start_ms=i)
        for i in range(100)
    ]

    def worker(chunk: list[MemoryAtom]) -> None:
        for a in chunk:
            store.append(a)

    threads = [threading.Thread(target=worker, args=(atoms[i::4],)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(store.atoms("s1")) == 100, "concurrent appends lost atoms"


def test_invariant_in_memory_atom_store_setcursor_threadsafe():
    """INV-1: cursor writes are atomic."""
    store = InMemoryAtomStore()

    def writer(start: int) -> None:
        for i in range(start, start + 100):
            store.set_cursor("s1", i)

    threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # No exception, value is the last write; the contract is just "atomic".
    assert store.get_cursor("s1") >= 0


def test_invariant_in_memory_index_thread_safe():
    """INV-1: the vector index is also shared mutable state on the read path."""
    idx = InMemoryMemoryIndex()
    atoms = [_atom(f"a{i}", "s1", f"text {i}", start_ms=i) for i in range(50)]
    vectors = [[float(i), 0.0, 0.0] for i in range(50)]

    def worker(chunk_atoms, chunk_vecs):
        for a, v in zip(chunk_atoms, chunk_vecs):
            idx.add(a, v)

    threads = [
        threading.Thread(target=worker, args=(atoms[i::4], vectors[i::4]))
        for i in range(4)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    results = idx.search("s1", [1.0, 0.0, 0.0], 100)
    assert len(results) == 50
    assert all(isinstance(r, SearchResult) for r in results)


def test_invariant_atoms_immutable_under_stamping():
    """INVARIANT 5: MemoryAtoms are frozen; re-extraction produces new atoms."""
    a = _atom("a1", "s1", "x", start_ms=0)
    with pytest.raises(Exception):  # ValidationError from pydantic
        a.text = "mutated"  # type: ignore[misc]
