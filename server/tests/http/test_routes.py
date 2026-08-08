"""Tests for /memory and /metrics routes (N3.3)."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from opensapien_server.agent.metrics import InMemoryMetricsRecorder
from opensapien_server.contracts.clock import FakeClock
from opensapien_server.contracts.id_generator import DeterministicIdGenerator
from opensapien_server.contracts.types import RetrieverContext, RetrievedContext, ScoredAtom
from opensapien_server.http.app import build_app
from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.store import InMemoryAtomStore


def _atom(atom_id: str, session_id: str = "s1", text: str = "x") -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id=session_id,
        source_event_id=f"e-{atom_id}",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )


def _scored(a: MemoryAtom, score: float = 0.9) -> ScoredAtom:
    return ScoredAtom(
        atom_id=a.atom_id, session_id=a.session_id, kind=a.kind,
        text=a.text, created_at=a.created_at, start_ms=a.start_ms, score=score,
    )


class StaticRetriever:
    def __init__(self, atoms):
        self._atoms = list(atoms)

    def retrieve(self, ctx: RetrieverContext) -> RetrievedContext:
        return RetrievedContext(
            atoms=tuple(self._atoms),
            retrieval_trace_id="trace-x",
            top_score=self._atoms[0].score if self._atoms else float("-inf"),
            lowest_score=self._atoms[-1].score if self._atoms else float("-inf"),
            returned_count=len(self._atoms),
        )


def _build_app(retriever, atom_store, metrics):
    return build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        retriever=retriever,
        atom_store=atom_store,
        metrics=metrics,
        id_generator=DeterministicIdGenerator(),
    )


async def _get(app, path: str):
    from aiohttp.test_utils import TestClient, TestServer
    headers = {"Authorization": "Bearer t"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path, headers=headers)
        return resp.status, await resp.json(), resp.headers


async def _get_text(app, path: str):
    from aiohttp.test_utils import TestClient, TestServer
    headers = {"Authorization": "Bearer t"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.get(path, headers=headers)
        return resp.status, await resp.text(), resp.headers


@pytest.mark.asyncio
async def test_memory_search_returns_results():
    a = _atom("a1", text="hello world")
    retriever = StaticRetriever([_scored(a)])
    status, body, _ = await _get(_build_app(retriever, InMemoryAtomStore(), InMemoryMetricsRecorder()), "/memory?q=hello")
    assert status == 200
    assert body["schema_version"] == "v1"
    assert body["query"] == "hello"
    assert body["returned_count"] == 1
    assert body["atoms"][0]["atom_id"] == "a1"


@pytest.mark.asyncio
async def test_memory_without_q_lists_instead_of_400():
    """Spec §1.2 replaced the 400: no `q` now means list mode.

    The Memories tab has no search box on first paint, so an empty query
    used to make the whole page unrenderable.
    """
    status, body, _ = await _get(_build_app(StaticRetriever([]), InMemoryAtomStore(), InMemoryMetricsRecorder()), "/memory")
    assert status == 200
    assert body["atoms"] == []
    assert body["next_cursor"] is None


@pytest.mark.asyncio
async def test_memory_search_session_id_filter():
    a = _atom("a1", session_id="s1", text="x")
    a2 = _atom("a2", session_id="s2", text="x")
    retriever = StaticRetriever([_scored(a), _scored(a2)])
    status, body, _ = await _get(
        _build_app(retriever, InMemoryAtomStore(), InMemoryMetricsRecorder()),
        "/memory?q=x&session_id=s1",
    )
    assert status == 200
    assert body["session_id"] == "s1"


@pytest.mark.asyncio
async def test_session_memory_returns_session_atoms():
    store = InMemoryAtomStore()
    store.append(_atom("a1", session_id="s1"))
    store.append(_atom("a2", session_id="s1"))
    store.append(_atom("a3", session_id="s2"))
    status, body, _ = await _get(
        _build_app(StaticRetriever([]), store, InMemoryMetricsRecorder()),
        "/sessions/s1/memory",
    )
    assert status == 200
    assert body["session_id"] == "s1"
    assert body["returned_count"] == 2
    assert {a["atom_id"] for a in body["atoms"]} == {"a1", "a2"}


@pytest.mark.asyncio
async def test_metrics_endpoint_returns_prometheus_text():
    metrics = InMemoryMetricsRecorder()
    metrics.increment("retrieval_hits_total")
    metrics.observe("planner_latency_ms", 12.0)
    status, text, headers = await _get_text(
        _build_app(StaticRetriever([]), InMemoryAtomStore(), metrics),
        "/metrics",
    )
    assert status == 200
    assert "retrieval_hits_total" in text
    assert "planner_latency_ms" in text
    assert "text/plain" in headers["Content-Type"]
