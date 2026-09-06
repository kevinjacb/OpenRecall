"""Tests for the new global Retriever (M3.2 / INV-7).

The Retriever is the single seam for "find relevant atoms for this query".
It is read-only — no mutation of the index, the atom store, or the
extraction cursor. Ordering is deterministic: score desc, then
created_at desc, then atom_id asc (INV-7).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.contracts.types import RetrieverContext
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.embeddings import Embedder
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import MemoryRetriever, Retriever
from openrecall_server.memory.scoring import FixedScorer, SimRecencyScorer


# --- test fakes --------------------------------------------------------------


class FakeDeterministicEmbedder:
    """Embedder that hashes each text to a stable 4-dim vector.

    Two identical texts produce identical vectors; two different texts
    produce different vectors (via the FNV-1a hash). This lets tests
    reason about similarity without depending on a real embedding model.
    """

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_hash_vec(t, self._dim) for t in texts]


def _hash_vec(text: str, dim: int) -> list[float]:
    """Token-bag projection: each character contributes to a fixed-dim vector.

    The same text always produces the same vector; texts that share
    characters produce *similar* (not orthogonal) vectors. This lets
    tests reason about cosine similarity without a real model.
    """
    out = [0.0] * dim
    for i, ch in enumerate(text.encode("utf-8")):
        out[i % dim] += float((ch * (i + 1)) % 251) / 251.0
    return out


def _atom(atom_id: str, session_id: str, text: str, created_at: datetime | None = None) -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=created_at or datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )


def _retriever(idx: InMemoryMemoryIndex, **kwargs) -> Retriever:
    clock = kwargs.pop("clock", FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)))
    ids = kwargs.pop("ids", DeterministicIdGenerator())
    scorer = kwargs.pop("scorer", SimRecencyScorer(half_life_s=1e12))  # effectively no decay
    return Retriever(
        embedder=FakeDeterministicEmbedder(),
        index=idx,
        scorer=scorer,
        clock=clock,
        ids=ids,
    )


def _add(idx: InMemoryMemoryIndex, atom_id: str, session_id: str, text: str, **kw) -> MemoryAtom:
    """Add an atom to the index using the test embedder's projection of its text.

    This keeps the test embedder and the stored vectors in lockstep so
    a Retriever over the index actually returns non-zero scores.
    """
    a = _atom(atom_id, session_id, text, **kw)
    idx.add(a, FakeDeterministicEmbedder().embed([text])[0])
    return a


# --- contract tests ----------------------------------------------------------


def test_retriever_returns_empty_when_index_empty():
    idx = InMemoryMemoryIndex()
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10, session_id=None))
    assert rc.atoms == ()
    assert rc.returned_count == 0
    assert rc.top_score == float("-inf")
    assert rc.lowest_score == float("-inf")


def test_retriever_global_no_filter_returns_all_sessions():
    idx = InMemoryMemoryIndex()
    _add(idx, "a1", "s1", "s1 text")
    _add(idx, "a2", "s2", "s2 text")
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="s1 text", limit=10, session_id=None))
    assert {a.session_id for a in rc.atoms} == {"s1", "s2"}


def test_retriever_session_filter_limits_results():
    idx = InMemoryMemoryIndex()
    _add(idx, "a1", "s1", "x")
    _add(idx, "a2", "s2", "x")
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10, session_id="s1"))
    assert {a.session_id for a in rc.atoms} == {"s1"}


def test_retriever_canonical_metadata():
    idx = InMemoryMemoryIndex()
    _add(idx, "a1", "s1", "x")
    r = _retriever(idx, ids=DeterministicIdGenerator())
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.retrieval_strategy == "sim_recency"
    assert rc.scorer_version == "v1"
    assert rc.index_name == "in_memory"
    assert rc.index_version == "v1"
    assert rc.retrieval_trace_id == "trace-0001"
    assert rc.session_filter is None
    assert rc.returned_count == 1
    assert rc.top_score > 0


def test_retriever_sessionless_atom_global_keeps_none_session():
    """A sessionless atom (session_id=None, e.g. an unmatched vision
    snapshot) indexed and retrieved globally must flow through ScoredAtom
    and to_provenance() without raising — both carry session_id=None.
    """
    idx = InMemoryMemoryIndex()
    scene = MemoryAtom(
        atom_id="scene:abc", session_id=None, source_event_id="blob:abc",
        kind="scene", text="a cat on the desk",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0, source_pipeline_version="vision",
    )
    idx.add(scene, FakeDeterministicEmbedder().embed(["a cat on the desk"])[0])
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="a cat on the desk", limit=10, session_id=None))
    assert rc.returned_count == 1
    [sa] = rc.atoms
    assert sa.session_id is None
    assert sa.provenance is not None
    assert sa.provenance.session_id is None
    assert sa.provenance.source_modality == "vision"


def test_retriever_respects_limit():
    idx = InMemoryMemoryIndex()
    for i in range(20):
        _add(idx, f"a{i}", "s1", f"text {i}")
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="text", limit=3))
    assert len(rc.atoms) == 3


def test_retriever_ordering_tiebreaker_score_desc():
    """INV-7: score desc is the first tiebreaker.

    a1 is the exact text of the query (cosine 1.0); a2 is a superset
    (cosine < 1.0). a1 should rank above a2.
    """
    idx = InMemoryMemoryIndex()
    _add(idx, "a1", "s1", "alpha bravo", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    _add(idx, "a2", "s1", "alpha bravo charlie", created_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="alpha bravo", limit=10))
    assert rc.atoms[0].atom_id == "a1"
    assert rc.atoms[0].score > rc.atoms[1].score


def test_retriever_ordering_tiebreaker_created_at_desc():
    """INV-7: created_at desc when scores tie (identical text)."""
    idx = InMemoryMemoryIndex()
    _add(idx, "a_old", "s1", "x", created_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    _add(idx, "a_new", "s1", "x", created_at=datetime(2026, 7, 5, tzinfo=timezone.utc))
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.atoms[0].atom_id == "a_new"


def test_retriever_ordering_tiebreaker_atom_id_asc():
    """INV-7: atom_id asc when both score and created_at tie."""
    when = datetime(2026, 7, 7, tzinfo=timezone.utc)
    idx = InMemoryMemoryIndex()
    _add(idx, "z_id", "s1", "x", created_at=when)
    _add(idx, "a_id", "s1", "x", created_at=when)
    r = _retriever(idx)
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.atoms[0].atom_id == "a_id"
    assert rc.atoms[1].atom_id == "z_id"


def test_retriever_is_deterministic():
    """INV-7: same inputs → same atom ordering, every time.

    The trace id increments per call (by design — every retrieval gets a
    fresh id), but the atom list is identical.
    """
    idx = InMemoryMemoryIndex()
    for i in range(10):
        _add(idx, f"a{i}", "s1", f"text {i}")
    r = _retriever(idx, ids=DeterministicIdGenerator())
    rc1 = r.retrieve(RetrieverContext(query_text="text 0", limit=5))
    rc2 = r.retrieve(RetrieverContext(query_text="text 0", limit=5))
    assert [a.atom_id for a in rc1.atoms] == [a.atom_id for a in rc2.atoms]
    assert rc1.retrieval_trace_id != rc2.retrieval_trace_id  # fresh id per call


def test_retriever_does_not_mutate_index():
    """INV-6: the retriever never mutates the index, the atom store, or the
    extraction cursor. We assert the index is unchanged across many calls."""
    idx = InMemoryMemoryIndex()
    _add(idx, "a1", "s1", "x")
    size_before = len(idx.search("", [1.0, 0.0, 0.0, 0.0], 100))
    r = _retriever(idx)
    for _ in range(50):
        r.retrieve(RetrieverContext(query_text="x", limit=10))
    size_after = len(idx.search("", [1.0, 0.0, 0.0, 0.0], 100))
    assert size_after == size_before


def test_retriever_recency_decreases_old_atoms():
    """A scorer with a short half-life should rank a recent atom above an old one."""
    when_now = datetime(2026, 7, 7, tzinfo=timezone.utc)
    clock = FakeClock(when_now)
    idx = InMemoryMemoryIndex()
    _add(idx, "a_old", "s1", "x", created_at=when_now - timedelta(days=30))
    _add(idx, "a_new", "s1", "x", created_at=when_now)
    r = _retriever(idx, clock=clock, scorer=SimRecencyScorer(half_life_s=7 * 24 * 3600))
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.atoms[0].atom_id == "a_new"


def test_recency_uses_conversation_time_not_ingest_time():
    """An atom said last night must not outrank one said an hour ago just
    because both were extracted at the same moment (spec D6)."""
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    extracted = now - timedelta(minutes=5)  # both learned 5 min ago

    # atom_ids are picked so that ascending atom_id order (the INV-7
    # tiebreaker of last resort) disagrees with the correct conversation-time
    # order — otherwise the atom_id tiebreak could mask a created_at-based bug.
    idx = InMemoryMemoryIndex()
    old_talk = MemoryAtom(
        atom_id="a_old", session_id="s1", source_event_id="e1", kind="fact",
        text="alpha topic", created_at=extracted, start_ms=0,
        occurred_at=now - timedelta(hours=14),  # said last night
    )
    new_talk = MemoryAtom(
        atom_id="z_new", session_id="s1", source_event_id="e2", kind="fact",
        text="alpha topic", created_at=extracted, start_ms=0,
        occurred_at=now - timedelta(hours=1),  # said an hour ago
    )
    idx.add(old_talk, FakeDeterministicEmbedder().embed(["alpha topic"])[0])
    idx.add(new_talk, FakeDeterministicEmbedder().embed(["alpha topic"])[0])

    r = _retriever(idx, clock=FakeClock(now))
    rc = r.retrieve(RetrieverContext(session_id="s1", query_text="alpha topic", limit=2))

    assert [a.atom_id for a in rc.atoms] == ["z_new", "a_old"]


def test_recency_tiebreaker_uses_conversation_time_not_ingest_time():
    """Isolates the sort tiebreaker from the age-computation site.

    FixedScorer ties every candidate's score at 1.0 regardless of age, so
    the age-computation fix (using timeline_at instead of created_at) can
    have no effect here — only the sort tiebreaker decides the order. Both
    atoms also share created_at, so a tiebreaker still keyed on created_at
    would tie too, and the result would fall through to atom_id asc
    ("a_old" before "z_new" — the wrong order). This test only passes if
    the tiebreaker itself reads timeline_at.
    """
    now = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)
    extracted = now - timedelta(minutes=5)  # both learned 5 min ago

    idx = InMemoryMemoryIndex()
    old_talk = MemoryAtom(
        atom_id="a_old", session_id="s1", source_event_id="e1", kind="fact",
        text="alpha topic", created_at=extracted, start_ms=0,
        occurred_at=now - timedelta(hours=14),  # said last night
    )
    new_talk = MemoryAtom(
        atom_id="z_new", session_id="s1", source_event_id="e2", kind="fact",
        text="alpha topic", created_at=extracted, start_ms=0,
        occurred_at=now - timedelta(hours=1),  # said an hour ago
    )
    idx.add(old_talk, FakeDeterministicEmbedder().embed(["alpha topic"])[0])
    idx.add(new_talk, FakeDeterministicEmbedder().embed(["alpha topic"])[0])

    r = _retriever(idx, clock=FakeClock(now), scorer=FixedScorer())
    rc = r.retrieve(RetrieverContext(session_id="s1", query_text="alpha topic", limit=2))

    assert [a.atom_id for a in rc.atoms] == ["z_new", "a_old"]


def test_memory_retriever_kept_for_backward_compat_with_warning():
    """M3.2: keep the old MemoryRetriever class, deprecate it."""
    idx = InMemoryMemoryIndex()
    idx.add(_atom("a1", "s1", "x"), [1.0, 0.0, 0.0, 0.0])
    with pytest.warns(DeprecationWarning, match="MemoryRetriever is deprecated"):
        mr = MemoryRetriever(embedder=FakeDeterministicEmbedder(), index=idx)
        results = mr.query("s1", "x", k=1)
    assert len(results) == 1
    assert results[0].vector == [1.0, 0.0, 0.0, 0.0]
