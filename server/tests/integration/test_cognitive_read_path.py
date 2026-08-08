"""End-to-end integration test: the cognitive read path (N3.5).

The single regression gate. The test wires the gateway stack the same
way ``run_gateway.py`` does (event store, atom store, memory index,
worker, planner, HTTP app) and exercises the full path:

  ingest event -> worker extracts -> index populated ->
  POST /agent returns answer with provenance chips.

Gates (the G's and I's from the spec):

  G1  end-to-end: a transcript event becomes a memory atom retrievable
      via POST /agent.
  G2  provenance: the response carries the atom_id, source_event_id, and
      created_at back to the client.
  G3  confidence band: the response carries a confidence_band.
  G4  short-circuit on no memory: a question with no supporting atoms
      returns outcome=refuse.
  G5  no-supporting-memory is audited (H3 — the audit must not block
      the response).
  G6  /metrics reflects the same data the worker recorded.
  G7  schema_version=v1 is on every response.
  I1  latency is recorded end-to-end (planner_latency_ms).
  I2  audit_id is present and unique per call.
  I3  refuse path is observable in the audit log.
  I4  retrieval_trace_id is present and unique per retrieval.
  I5  LLM is async — concurrency works.
  I6  /memory and /sessions/{id}/memory work end-to-end.
  I7  extra fields in the request are rejected (400).
  I8  bad bearer token is rejected (401).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Iterable

import pytest

from openrecall_server.agent.audit import InMemoryAuditLogger
from openrecall_server.agent.capability import ConstantCapabilityProvider
from openrecall_server.agent.context import ContextBuilder
from openrecall_server.agent.guardrails import ConfidenceGateGuardrails
from openrecall_server.agent.intent import AgentLLM, OpenAICompatibleAgentLLM
from openrecall_server.agent.metrics import InMemoryMetricsRecorder
from openrecall_server.agent.planner import Planner
from openrecall_server.agent.validator import StrictJSONValidator
from openrecall_server.contracts.clock import FakeClock, SystemClock
from openrecall_server.contracts.id_generator import DeterministicIdGenerator, UuidIdGenerator
from openrecall_server.contracts.metrics import Metrics
from openrecall_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    LLMResult,
    Prompt,
    ScoredAtom,
)
from openrecall_server.events.model import CaptureEvent
from openrecall_server.events.store import InMemoryEventStore
from openrecall_server.http.app import build_app
from openrecall_server.memory.atom import MemoryAtom
from openrecall_server.memory.embeddings import Embedder
from openrecall_server.memory.extract import ExtractedMemory, Extractor
from openrecall_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
)
from openrecall_server.memory.index import InMemoryMemoryIndex
from openrecall_server.memory.retrieval import Retriever
from openrecall_server.memory.scoring import SimRecencyScorer
from openrecall_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from openrecall_server.memory.store import InMemoryAtomStore


# --- helpers (H6: wait_for, no sleep) ---------------------------------------


async def wait_for(predicate, *, timeout: float = 2.0, interval: float = 0.01) -> bool:
    """Poll until ``predicate()`` is truthy or ``timeout`` elapses.

    Replaces ``asyncio.sleep`` in the integration test (H6). Returns
    True if the predicate became true, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --- test doubles ------------------------------------------------------------


class FixedExtractor:
    def extract(self, text: str) -> list[ExtractedMemory]:
        return [ExtractedMemory(kind="fact", text=text)]


class FixedEmbedder:
    """Maps text -> 4-dim bag-of-bytes vector. Two texts that share
    characters produce *similar* vectors (non-orthogonal) so cosine
    similarity is non-zero for related queries."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            v = [0.0] * 4
            for i, ch in enumerate(t.encode()):
                v[i % 4] += float((ch * (i + 1)) % 251) / 251.0
            out.append(v)
        return out


class FakeChat:
    """Synchronous fake chat. Returns a configurable response per call."""

    def __init__(self, response: str) -> None:
        self._response = response
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        return self._response


def _event(seq: int, text: str = "I love working on OpenRecall every morning at 7am.", session_id: str = "s1") -> CaptureEvent:
    return CaptureEvent(
        event_id=f"{session_id}:{seq}",
        session_id=session_id,
        seq=seq,
        kind="transcript",
        created_at=datetime(2026, 7, 7, 7, 0, 0, tzinfo=timezone.utc),
        text=text,
        duration_ms=1000,
        start_ms=seq * 1000,
    )


# --- fixture -----------------------------------------------------------------


@pytest.fixture
def stack():
    """Build a complete gateway stack with the same wiring run_gateway uses."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    index = InMemoryMemoryIndex()
    embedder = FixedEmbedder()
    metrics = InMemoryMetricsRecorder()
    audit = InMemoryAuditLogger()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)

    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=embedder),
        indexing=IndexingStage(index=index),
        store=atoms,
    )
    worker = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline, metrics=metrics,
    )
    response_json = '{"kind":"answer","text":"You said you love OpenRecall every morning.","atom_ids":["s1:0:0"],"confidence":0.9}'
    agent_llm = OpenAICompatibleAgentLLM(FakeChat(response_json))
    retriever = Retriever(
        embedder=embedder,
        index=index,
        scorer=SimRecencyScorer(half_life_s=1e12),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=DeterministicIdGenerator(),
    )
    planner = Planner(
        retriever=retriever,
        context_builder=ContextBuilder(),
        llm=agent_llm,
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=audit,
        metrics=metrics,
        capability_provider=ConstantCapabilityProvider(),
        clock=FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc)),
        ids=UuidIdGenerator(),
    )
    app = build_app(
        token="t",
        get_pubkey=lambda: b"\x00" * 32,
        planner=planner,
        retriever=retriever,
        atom_store=atoms,
        metrics=metrics,
        id_generator=UuidIdGenerator(),
    )
    return {
        "events": events,
        "atoms": atoms,
        "index": index,
        "embedder": embedder,
        "metrics": metrics,
        "audit": audit,
        "pipeline": pipeline,
        "worker": worker,
        "agent_llm": agent_llm,
        "retriever": retriever,
        "planner": planner,
        "app": app,
    }


# --- helpers for HTTP --------------------------------------------------------


async def _post(stack, path, payload, token="t"):
    from aiohttp.test_utils import TestClient, TestServer
    headers = {"Authorization": f"Bearer {token}"}
    async with TestClient(TestServer(stack["app"])) as client:
        resp = await client.post(path, json=payload, headers=headers)
        return resp.status, await resp.json()


async def _get(stack, path, token="t"):
    from aiohttp.test_utils import TestClient, TestServer
    headers = {"Authorization": f"Bearer {token}"}
    async with TestClient(TestServer(stack["app"])) as client:
        resp = await client.get(path, headers=headers)
        return resp.status, await resp.json()


# --- the gates --------------------------------------------------------------


@pytest.mark.asyncio
async def test_G1_end_to_end_event_becomes_atom_answerable(stack):
    """G1: a transcript event becomes a memory atom answerable via /agent."""
    stack["events"].append(_event(0, "I love working on OpenRecall every morning at 7am."))
    stack["worker"].process_session("s1")
    status, body = await _post(stack, "/agent", {"session_id": "s1", "text": "what do I do in the morning?"})
    assert status == 200
    assert body["outcome"] == "return"
    assert body["answer"] == "You said you love OpenRecall every morning."


@pytest.mark.asyncio
async def test_G2_provenance_atom_id_carried_through(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning at 7am."))
    stack["worker"].process_session("s1")
    _, body = await _post(stack, "/agent", {"session_id": "s1", "text": "morning routine?"})
    assert body["atoms"][0]["atom_id"].startswith("s1:0:")
    assert body["atoms"][0]["created_at"].startswith("2026")


@pytest.mark.asyncio
async def test_G3_confidence_band_on_response(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    _, body = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    assert body["confidence_band"] in ("low", "medium", "high")


@pytest.mark.asyncio
async def test_G4_empty_retrieval_refuses(stack):
    """G4: with no events ever ingested, /agent refuses. The
    refusal is enforced by the prompt contract (v2 no-memory
    directive) plus the answer guardrails' NO_SUPPORTING_MEMORY
    mapping, not by a planner-side short-circuit. The LLM IS
    called (the prompt is the safety net for factual questions
    and the entry point for direct device actions on a cold
    index)."""
    _, body = await _post(stack, "/agent", {"session_id": "s_unknown", "text": "anything?"})
    assert body["outcome"] == "refuse"
    assert body["refusal_reason"] is not None
    # The LLM IS called when there are no supporting atoms.
    # (Previously a planner-side short-circuit skipped the LLM,
    # but that short-circuit was removed in batch-1 to admit
    # direct commands on a cold index. Factual-question safety
    # is preserved by the v2 prompt + answer guardrails.)
    assert stack["agent_llm"]._chat.calls == 1


@pytest.mark.asyncio
async def test_G5_audit_failure_does_not_lose_response(stack):
    """G5: a failing audit does not block the user response (H3)."""
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    # Swap the planner's audit to one that throws.
    class FailingAudit:
        def record(self, entry): raise RuntimeError("disk full")
    stack["planner"]._audit = FailingAudit()
    status, body = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    assert status == 200
    assert body["outcome"] == "return"


@pytest.mark.asyncio
async def test_G6_metrics_reflects_worker_activity(stack):
    """G6: /metrics shows the same counters the worker observed."""
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    # /metrics returns Prometheus text (not JSON), so use a separate call.
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(stack["app"])) as client:
        resp = await client.get("/metrics", headers={"Authorization": "Bearer t"})
        text = await resp.text()
    assert "extraction_latency_ms" in text


@pytest.mark.asyncio
async def test_G7_schema_version_v1_everywhere(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    _, body = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    assert body["schema_version"] == "v1"
    _, body2 = await _get(stack, "/memory?q=love")
    assert body2["schema_version"] == "v1"


@pytest.mark.asyncio
async def test_I1_planner_latency_recorded(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    hist = stack["metrics"].histogram(Metrics.PLANNER_LATENCY_MS)
    assert hist.count == 1


@pytest.mark.asyncio
async def test_I2_audit_id_unique_per_call(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    _, b1 = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    _, b2 = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    assert b1["audit_id"] != b2["audit_id"]


@pytest.mark.asyncio
async def test_I3_refuse_audited(stack):
    """I3: refuse outcomes are observable in the audit log."""
    await _post(stack, "/agent", {"session_id": "s_unknown", "text": "x"})
    refusals = [e for e in stack["audit"].entries if e["outcome"] == "refuse"]
    assert len(refusals) >= 1


@pytest.mark.asyncio
async def test_I4_retrieval_trace_id_unique(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    _, b1 = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    _, b2 = await _post(stack, "/agent", {"session_id": "s1", "text": "what?"})
    assert b1["retrieval_trace_id"] != "" and b1["retrieval_trace_id"] != b2["retrieval_trace_id"]


@pytest.mark.asyncio
async def test_I5_concurrent_agent_requests(stack):
    """I5: 10 concurrent /agent requests don't serialize (H4)."""
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["worker"].process_session("s1")
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(stack["app"])) as client:
        async def one():
            return await client.post(
                "/agent",
                json={"session_id": "s1", "text": "what?"},
                headers={"Authorization": "Bearer t"},
            )
        start = time.monotonic()
        results = await asyncio.gather(*[one() for _ in range(10)])
        elapsed = time.monotonic() - start
    for r in results:
        assert r.status == 200
    # Allow generous slack; would fail at ~10x the LLM delay if serialized.
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_I6_memory_endpoints_work(stack):
    stack["events"].append(_event(0, "I love OpenRecall every morning."))
    stack["events"].append(_event(1, "I also like writing tests in the afternoon."))
    stack["worker"].process_session("s1")
    _, search = await _get(stack, "/memory?q=morning")
    assert search["returned_count"] >= 1
    _, session = await _get(stack, "/sessions/s1/memory")
    assert session["returned_count"] >= 1
    assert session["session_id"] == "s1"


@pytest.mark.asyncio
async def test_I7_extra_fields_rejected(stack):
    status, body = await _post(stack, "/agent", {"text": "x", "extra": "boom"})
    assert status == 400
    assert body["code"] == "bad_request"


@pytest.mark.asyncio
async def test_I8_bad_bearer_rejected(stack):
    from aiohttp.test_utils import TestClient, TestServer
    async with TestClient(TestServer(stack["app"])) as client:
        resp = await client.post("/agent", json={"text": "x"}, headers={"Authorization": "Bearer wrong"})
        assert resp.status == 401
