"""Tests for the async AgentLLM (N2.1 / H4)."""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from openrecall_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    IssueCommandPayload,
    LLMResult,
    Prompt,
)
from openrecall_server.agent.intent import OpenAICompatibleAgentLLM


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
        system="You are OpenRecall.",
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


# --- issue_command parsing (P2-commands) -----------------------------------


@pytest.mark.asyncio
async def test_reason_parses_issue_command_with_payload_from_v2_prompt():
    """The v2 system prompt contracts the LLM to emit, for a direct
    command request, exactly this JSON shape:

        {
          "kind": "issue_command",
          "text": "",
          "atom_ids": [],
          "confidence": 0.85,
          "command": {
            "command_type": "record_video",
            "params": {"duration_s": 3},
            "idempotency_key": "record_video_3s"
          }
        }

    Previously, _parse silently dropped the ``command`` field, so
    ``AgentAction.command`` was always ``None`` even when the LLM
    produced the right shape. The Validator then had no payload to
    validate and the Planner had nothing to dispatch. This test pins
    the new contract: the command payload is parsed and surfaces on
    ``AgentAction.command``.
    """
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.85,
        "command": {
            "command_type": "record_video",
            "params": {"duration_s": 3},
            "idempotency_key": "record_video_3s",
        },
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parse_error is None, f"unexpected parse error: {result.parse_error}"
    assert result.parsed is not None
    assert result.parsed.kind == AgentActionKind.ISSUE_COMMAND
    # Per the v2 prompt contract: text and atom_ids are empty for
    # issue_command. The parser must NOT require them to be present.
    assert result.parsed.text == ""
    assert result.parsed.atom_ids == ()
    # The structured payload must be retained.
    assert result.parsed.command is not None
    assert result.parsed.command.command_type == "record_video"
    assert result.parsed.command.params == {"duration_s": 3}
    assert result.parsed.command.idempotency_key == "record_video_3s"
    # And it's a fully-validated IssueCommandPayload (frozen, extra=forbid).
    assert isinstance(result.parsed.command, IssueCommandPayload)


@pytest.mark.asyncio
async def test_reason_parses_issue_command_without_params():
    """capture_photo, start_audio, and stop_audio have no params.
    The LLM may omit the field or send ``{}``; both must parse."""
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.9,
        "command": {
            "command_type": "capture_photo",
            "params": {},
            "idempotency_key": "capture_photo_1",
        },
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parse_error is None
    assert result.parsed is not None
    assert result.parsed.command is not None
    assert result.parsed.command.command_type == "capture_photo"
    assert result.parsed.command.params == {}


@pytest.mark.asyncio
async def test_reason_parses_issue_command_with_default_params_field():
    """The v2 prompt says the params field is 'omit if no params'.
    The parser must accept a missing params field by defaulting to {}."""
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.9,
        "command": {
            "command_type": "start_audio",
            "idempotency_key": "start_audio_1",
        },
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parse_error is None
    assert result.parsed is not None
    assert result.parsed.command is not None
    assert result.parsed.command.params == {}


@pytest.mark.asyncio
async def test_reason_returns_parse_error_when_issue_command_has_no_payload():
    """A kind=issue_command reply without a ``command`` field is a
    parse failure — the validator cannot accept a command action
    without a payload. Returning ``parsed=None`` lets the Validator
    surface a clear error instead of dispatching a None."""
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.85,
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert result.parse_error is not None
    assert "command" in result.parse_error.lower()


@pytest.mark.asyncio
async def test_reason_returns_parse_error_when_issue_command_has_unknown_type():
    """The 5-type allowlist is enforced by IssueCommandPayload (the
    Literal type + the StrictCommandValidator downstream). The parser
    must surface an unknown command_type as a parse error rather
    than silently constructing an invalid payload."""
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.9,
        "command": {
            "command_type": "launch_missiles",
            "params": {},
            "idempotency_key": "x",
        },
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert result.parse_error is not None


@pytest.mark.asyncio
async def test_reason_returns_parse_error_when_issue_command_missing_idempotency_key():
    """idempotency_key is required (the re-prompt dedup relies on it)."""
    raw = json.dumps({
        "kind": "issue_command",
        "text": "",
        "atom_ids": [],
        "confidence": 0.9,
        "command": {
            "command_type": "capture_photo",
            "params": {},
        },
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    assert result.parsed is None
    assert result.parse_error is not None


@pytest.mark.asyncio
async def test_reason_does_not_carry_command_field_on_answer():
    """An answer reply must NOT carry a command field — the parser
    must leave AgentAction.command = None for non-command kinds even
    if the LLM hallucinates one. IssueCommandPayload's extra='forbid'
    would normally reject extras, but a stray 'command' key on a
    non-issue_command kind must be silently ignored (the field is
    optional in the model)."""
    raw = json.dumps({
        "kind": "answer",
        "text": "you said hi",
        "atom_ids": ["a1"],
        "confidence": 0.9,
        "command": {"command_type": "capture_photo", "idempotency_key": "x"},
    })
    llm = OpenAICompatibleAgentLLM(FakeChat(raw))
    result = await llm.reason(_prompt())
    # We accept either a successful parse with command=None (silent
    # ignore) OR a parse error (extra='forbid' on IssueCommandPayload
    # propagated). The contract is: an answer reply is never a
    # command, period. Pin the stronger behavior.
    assert result.parsed is not None
    assert result.parsed.kind == AgentActionKind.ANSWER
    assert result.parsed.command is None


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
    from openrecall_server.agent.intent import AgentLLM
    assert isinstance(OpenAICompatibleAgentLLM(FakeChat()), AgentLLM)
