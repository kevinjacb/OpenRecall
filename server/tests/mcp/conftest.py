"""Fixtures for the MCP tool-surface tests.

The brief's fixture skeleton specifies the `aiohttp_client` pytest fixture,
but this codebase does not install `pytest-aiohttp`; existing HTTP tests use
`aiohttp.test_utils.TestClient/TestServer` directly (see
`tests/http/test_auth.py`, `tests/mcp/test_principal.py`). Only the
client-construction mechanism is adapted; the fixture's contract —
`(client, ledger)` — is the brief's.

The app is built **with both tokens**. That is load-bearing: a tokenless app
gets the *relay* principal from the middleware, which `may_reach` then blocks
from `/mcp` (403), so a tokenless app cannot exercise the MCP surface at all.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from aiohttp.test_utils import TestClient, TestServer

from openrecall_server.agent.capability import ConstantCapabilityProvider
from openrecall_server.contracts.clock import FakeClock, SystemClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.http.app import build_app
from openrecall_server.ingest.speaker_config import SpeakerConfig
from openrecall_server.mcp.ledger import RequestLedger
from openrecall_server.mcp.tools import build_registry
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import Retriever
from openrecall_server.memory.scoring import SimRecencyScorer
from openrecall_server.memory.speaker_registry import InMemorySpeakerRegistry
from openrecall_server.memory.store import InMemoryAtomStore
from openrecall_server.sessions.index import SessionIndex

_NOW = datetime(2026, 9, 6, tzinfo=timezone.utc)

# More than MAX_LIMIT, so `test_memory_search_limit_is_capped_at_20` is a real
# test: with 25 matching atoms an uncapped limit=500 would return 25.
_SEEDED_ATOMS = 25


class _HashEmbedder:
    """Deterministic 4-dim hash embedder (same shape as
    `tests/memory/test_retriever.py`'s `FakeDeterministicEmbedder`).

    All components are non-negative, so cosine similarity against any query
    is > 0 and the retriever's `score <= 0` filter never silently empties the
    corpus.
    """

    def __init__(self, dim: int = 4) -> None:
        self._dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def _vec(self, text: str) -> list[float]:
        out = [0.0] * self._dim
        for i, ch in enumerate(text.encode("utf-8")):
            out[i % self._dim] += float((ch * (i + 1)) % 251) / 251.0
        return out


class _SpyRetriever:
    """Retriever wrapper that records every `retrieve` call.

    Lets a test assert that the ledger guard ran *before* any store was
    touched. Without this, dropping `_require_open` would still surface
    "request not open" — `record_atoms` raises the identical message on the
    way out — so the guard test would pass against unguarded code.
    """

    def __init__(self, inner) -> None:
        self._inner = inner
        self.calls: list = []

    def retrieve(self, ctx):
        self.calls.append(ctx)
        return self._inner.retrieve(ctx)


def _atom(i: int) -> MemoryAtom:
    return MemoryAtom(
        atom_id=f"a{i:02d}",
        session_id="s1",
        source_event_id=f"e{i:02d}",
        kind="fact",
        text=f"the roadmap for quarter {i} covers the roadmap review",
        created_at=_NOW - timedelta(minutes=i),
        start_ms=i * 1000,
    )


@pytest.fixture
async def mcp_env():
    """Returns (client, ledger) — the app under test and the ledger it was
    built with."""
    ledger = RequestLedger(SystemClock())

    atom_store = InMemoryAtomStore()
    index = InMemoryMemoryIndex()
    embedder = _HashEmbedder()
    for i in range(_SEEDED_ATOMS):
        atom = _atom(i)
        atom_store.append(atom)
        index.add(atom, embedder.embed([atom.text])[0])

    retriever = _SpyRetriever(Retriever(
        embedder=embedder,
        index=index,
        scorer=SimRecencyScorer(half_life_s=1e12),  # effectively no decay
        clock=FakeClock(_NOW),
        ids=DeterministicIdGenerator(),
    ))

    registry = build_registry(
        retriever=retriever,
        atom_store=atom_store,
        session_index=SessionIndex(),
        speaker_registry=InMemorySpeakerRegistry(SpeakerConfig()),
        capability_provider=ConstantCapabilityProvider(),
        ledger=ledger,
    )
    app = build_app(
        token="aaa", hermes_token="bbb", get_pubkey=lambda: bytes(32),
        mcp_registry=registry, mcp_ledger=ledger,
        # Same object the registry holds — production wires it here too, and
        # it gives tests a handle on the spy via client.app["sense_retriever"].
        retriever=retriever,
    )
    server = TestServer(app)
    client = TestClient(server)
    await client.start_server()
    yield client, ledger
    await client.close()
