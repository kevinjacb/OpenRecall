import asyncio
from datetime import datetime, timezone

import pytest

from openrecall_server.agent.hermes_planner import FakeTransport, HermesPlanner
from openrecall_server.contracts.types import (
    PlannerContext, PlannerOutcome, Proactive, RejectionReason, UserRequest,
)
from openrecall_server.mcp.ledger import AgentResponse, RequestLedger


class FakeClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now


class SeqIds:
    def __init__(self): self.n = 0
    def new(self):
        self.n += 1
        return f"id-{self.n}"


def _planner(on_run, **kw):
    ledger = RequestLedger(FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc)))
    p = HermesPlanner(
        transport=FakeTransport(on_run), ledger=ledger,
        clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc)),
        ids=SeqIds(), **kw,
    )
    return p, ledger


def _ctx(trigger=None):
    return PlannerContext(
        request_id="req-1",
        trigger=trigger or UserRequest(request_id="req-1", text="what did I say?"),
        session_id="s1", limit=10,
    )


async def test_result_is_built_from_the_ledger_not_stdout():
    async def on_run(prompt, request_id):
        ledger.record_response(request_id, AgentResponse(
            kind="answer", text="from the ledger", atom_ids=(),
            confidence=0.8, command_id=None, memory_atom_id=None,
            reminder_id=None))
        return "THIS STDOUT MUST BE IGNORED"

    p, ledger = _planner(on_run)
    out = await p.plan(_ctx())
    assert out.answer == "from the ledger"
    assert out.outcome is PlannerOutcome.RETURN
    assert "STDOUT" not in (out.answer or "")


async def test_cited_atoms_reach_the_result():
    async def on_run(prompt, request_id):
        ledger.record_atoms(request_id, ["a1", "a2"])
        ledger.record_response(request_id, AgentResponse(
            kind="answer", text="ok", atom_ids=("a1",), confidence=0.9,
            command_id=None, memory_atom_id=None, reminder_id=None))
        return ""

    p, ledger = _planner(on_run)
    out = await p.plan(_ctx())
    assert out.atom_ids == ("a1",)


async def test_no_response_degrades_to_unverified():
    async def on_run(prompt, request_id):
        return "chatty text but no agent.respond call"

    p, ledger = _planner(on_run)
    out = await p.plan(_ctx())
    assert out.outcome is PlannerOutcome.RETURN_WITH_UNCERTAINTY
    assert out.confidence_band == "unverified"
    assert out.atom_ids == ()


async def test_no_response_refuses_under_strict_provenance():
    async def on_run(prompt, request_id):
        return "still no agent.respond call"

    p, ledger = _planner(on_run, strict_provenance=True)
    out = await p.plan(_ctx())
    assert out.outcome is PlannerOutcome.REFUSE
    assert out.refusal_reason is RejectionReason.PROVENANCE_MISSING


async def test_ledger_entry_is_closed_even_when_the_transport_raises():
    seen = {}

    async def on_run(prompt, request_id):
        seen["id"] = request_id
        raise RuntimeError("transport exploded")

    p, ledger = _planner(on_run)
    with pytest.raises(RuntimeError):
        await p.plan(_ctx())
    assert ledger.get(seen["id"]) is None, "entry leaked after a failed run"


async def test_ledger_entry_is_closed_after_a_normal_run():
    seen = {}

    async def on_run(prompt, request_id):
        seen["id"] = request_id
        return ""

    p, ledger = _planner(on_run)
    await p.plan(_ctx())
    assert ledger.get(seen["id"]) is None


async def test_proactive_trigger_opens_a_proactive_entry():
    seen = {}

    async def on_run(prompt, request_id):
        seen["may_issue"] = ledger.may_issue_command(request_id)
        return ""

    p, ledger = _planner(on_run)
    ctx = _ctx(Proactive(request_id="req-1", event_id="e1", transcript=""))
    await p.plan(ctx)
    assert seen["may_issue"] is False, \
        "a proactive request must not be allowed to issue device commands"


async def test_user_request_entry_may_issue_commands():
    seen = {}

    async def on_run(prompt, request_id):
        seen["may_issue"] = ledger.may_issue_command(request_id)
        return ""

    p, ledger = _planner(on_run)
    await p.plan(_ctx())
    assert seen["may_issue"] is True


async def test_timeout_raises_and_closes_the_entry():
    seen = {}

    async def on_run(prompt, request_id):
        seen["id"] = request_id
        await asyncio.sleep(10)
        return ""

    p, ledger = _planner(on_run, timeout_s=0.01)
    with pytest.raises(asyncio.TimeoutError):
        await p.plan(_ctx())
    assert ledger.get(seen["id"]) is None


async def test_prompt_carries_the_trigger_text_and_the_request_id():
    p, ledger = _planner(lambda *_: None)
    prompt = p.build_prompt(_ctx())
    assert "what did I say?" in prompt
    assert "req-1" in prompt
    assert "agent.respond" in prompt


def test_hermes_planner_satisfies_plannerlike():
    from openrecall_server.agent.planner import PlannerLike
    p, _ = _planner(lambda *_: None)
    assert isinstance(p, PlannerLike)
