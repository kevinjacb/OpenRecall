"""Tests for the async AgentLLM (N2.1 / H4)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    LLMResult,
    Prompt,
)
from sense_server.agent.intent import OpenAICompatibleAgentLLM


class FakeChat:
    """Synchronous chat model with a controllable response + delay.

    ``delay`` lets us assert the H4 concurrency property: ten parallel
    ``reason`` calls with delay=0.1 should complete in ~0.1s, not ~1.0s.
    """

    def __init__(self, response: str = "", delay: float = 0.0) -> None:
        self._response = response
        self._delay = delay
        self.calls = 0

    def complete(self, system: str, user: str) -> str:
        self.calls += 1
        if self._delay > 0:
            time.sleep(self._delay)
        return self._response


def _prompt() -> Prompt:
    return Prompt(
        system="You are Sense.",
        user='{"session_id":"s1","text":"hi","limit":10}',
        system_prompt_version="v1",
        context_builder_version="v1",
    )


@pytest.mark.asyncio
async def test_reason_parses_valid_answer():
    raw = json.dumps({
        "kind": "answer",
        "text": "You said hi.",
        "atom_ids": ["a1"],
        "confidence": 0.9,
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is not None
    assert result.parsed.kind == AgentActionKind.ANSWER
    assert result.parsed.text == "You said hi."
    assert result.parsed.atom_ids == ("a1",)
    assert result.parsed.confidence == 0.9
    assert result.parse_error is None


@pytest.mark.asyncio
async def test_reason_parses_no_memory():
    raw = json.dumps({
        "kind": "no_memory",
        "text": "No relevant memory.",
        "atom_ids": [],
        "confidence": 0.7,
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is not None
    assert result.parsed.kind == AgentActionKind.NO_MEMORY


@pytest.mark.asyncio
async def test_reason_returns_parse_error_on_invalid_json():
    llm = OpenAICompatibleAgentLLM(FakeChat("not json"))
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert result.parse_error is not None
    assert "json" in result.parse_error.lower()


@pytest.mark.asyncio
async def test_reason_returns_parse_error_on_empty_response():
    llm = OpenAICompatibleAgentLLM(FakeChat(""))
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert "empty" in (result.parse_error or "").lower()


@pytest.mark.asyncio
async def test_reason_returns_parse_error_on_unknown_kind():
    raw = json.dumps({
        "kind": "shrug",
        "text": "x",
        "atom_ids": [],
        "confidence": 0.5,
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is None


@pytest.mark.asyncio
async def test_reason_returns_transport_error_on_exception():
    class Boom:
        def complete(self, system, user):
            raise RuntimeError("upstream timeout")

    llm = OpenAICompatibleAgentLLM(Boom())
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert "transport" in (result.parse_error or "").lower()


@pytest.mark.asyncio
async def test_concurrent_reason_calls_dont_serialize_h4():
    """H4: ten parallel reason() calls with 50ms blocking LLM should
    complete in ~50ms, not 500ms."""
    llm = OpenAICompatibleAgentLLM(FakeChat(response=json.dumps({
        "kind": "no_memory",
        "text": "x",
        "atom_ids": [],
        "confidence": 0.9,
    }), delay=0.05))
    start = time.monotonic()
    results = await asyncio.gather(*[llm.reason(_prompt()) for _ in range(10)])
    elapsed = time.monotonic() - start
    assert all(r.parsed is not None for r in results)
    # Allow generous slack for scheduling; the test would clearly fail
    # at ~500ms if the calls were serialized.
    assert elapsed < 0.4, f"concurrent calls serialized (elapsed={elapsed:.2f}s)"


def test_agent_llm_protocol_satisfied():
    from sense_server.agent.intent import AgentLLM
    assert isinstance(OpenAICompatibleAgentLLM(FakeChat()), AgentLLM)
