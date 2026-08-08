"""Tests for POST /agent (N2.2)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Iterable

import pytest

from openrecall_server.agent.audit import InMemoryAuditLogger
from openrecall_server.agent.capability import ConstantCapabilityProvider
from openrecall_server.agent.context import ContextBuilder
from openrecall_server.agent.guardrails import ConfidenceGateGuardrails
from openrecall_server.agent.intent import AgentLLM
from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.agent.planner import Planner
from openrecall_server.agent.validator import StrictJSONValidator
from openrecall_server.contracts.clock import FakeClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator
from openrecall_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    LLMResult,
    Prompt,
    RetrieverContext,
    RetrievedContext,
    ScoredAtom,
)
from openrecall_server.http.app import build_app
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import Retriever
from openrecall_server.memory.scoring import SimRecencyScorer
from openrecall_server.memory.store import InMemoryAtomStore


def _atom(atom_id: str, text: str, score: float = 0.9) -> ScoredAtom:
    return ScoredAtom(
        atom_id=atom_id,
        session_id="s1",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        score=score,
    )


class FixedLLM:
    def __init__(self, parsed):
        self._parsed = parsed

    async def reason(self, prompt: Prompt) -> LLMResult:
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


def _build_with_planner(llm_parsed, atoms=(_atom("a1", "x"),)):
    index = InMemoryMemoryIndex()
    for a in atoms:
        index.add(a.to_provenance() and a, [0.0, 0.0, 0.0]) if False else None
    # Use a richer embedder to populate vectors.
    class E:
        def embed(self, texts):
            return [[0.0, 0.0, 0.0] for _ in texts]
    # We use a simpler retriever: it reads from a static atoms list.
    class StaticRetriever:
        def __init__(self, atoms):
            self._atoms = list(atoms)
        def retrieve(self, ctx):
            return RetrievedContext(
                atoms=tuple(self._atoms),
                retrieval_trace_id="trace-static",
                top_score=self._atoms[0].score if self._atoms else float("-inf"),
                lowest_score=self._atoms[-1].score if self._atoms else float("-inf"),
                returned_count=len(self._atoms),
            )
    metrics = InMemoryMetricsRecorder()
    planner = Planner(
        retriever=StaticRetriever(atoms),
        context_builder=ContextBuilder(),
        llm=FixedLLM(llm_parsed),
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=InMemoryAuditLogger(),
        metrics=metrics,
        capability_provider=ConstantCapabilityProvider(),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        planner=planner,
        retriever=StaticRetriever(atoms),
        atom_store=InMemoryAtomStore(),
        metrics=metrics,
        id_generator=DeterministicIdGenerator(),
    )
    return app


async def _post_agent(app, payload: dict, token: str = "t"):
    from aiohttp.test_utils import TestClient, TestServer
    headers = {"Authorization": f"Bearer {token}"}
    async with TestClient(TestServer(app)) as client:
        resp = await client.post("/agent", json=payload, headers=headers)
        return resp.status, await resp.json()


@pytest.mark.asyncio
async def test_post_agent_returns_answer_with_cited_atoms():
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=("a1",),
        confidence=0.9,
    )
    app = _build_with_planner(parsed)
    status, body = await _post_agent(app, {"text": "what?"})
    assert status == 200
    assert body["outcome"] == "return"
    assert body["answer"] == "x"
    assert body["schema_version"] == "v1"
    assert body["request_id"] != ""
    assert body["retrieval_trace_id"] == "trace-static"
    assert body["audit_id"] != ""


@pytest.mark.asyncio
async def test_post_agent_returns_refuse_when_no_atoms():
    parsed = AgentAction(
        kind=AgentActionKind.ANSWER,
        text="x",
        atom_ids=("a1",),
        confidence=0.9,
    )
    app = _build_with_planner(parsed, atoms=())
    status, body = await _post_agent(app, {"text": "what?"})
    assert status == 200
    assert body["outcome"] == "refuse"
    assert body["answer"] is None


@pytest.mark.asyncio
async def test_post_agent_returns_400_on_invalid_body():
    app = _build_with_planner(None)
    status, body = await _post_agent(app, {"text": ""})
    assert status == 400
    assert body["code"] == "bad_request"


@pytest.mark.asyncio
async def test_post_agent_returns_400_on_missing_text():
    app = _build_with_planner(None)
    status, body = await _post_agent(app, {})
    assert status == 400


@pytest.mark.asyncio
async def test_post_agent_returns_400_on_extra_field():
    app = _build_with_planner(None)
    status, body = await _post_agent(app, {"text": "x", "extra": "boom"})
    assert status == 400


@pytest.mark.asyncio
async def test_post_agent_session_id_optional():
    parsed = AgentAction(kind=AgentActionKind.ANSWER, text="x", atom_ids=("a1",), confidence=0.9)
    app = _build_with_planner(parsed)
    status, body = await _post_agent(app, {"text": "what?"})
    assert status == 200
    # No session_id in the request → handled as global retrieval.
    assert body["outcome"] == "return"
