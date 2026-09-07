import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from openrecall_server.agent.fallback_planner import CircuitBreaker, FallbackPlanner
from openrecall_server.contracts.types import (
    PlannerContext, PlannerOutcome, PlannerResult, UserRequest,
)


class FakeClock:
    def __init__(self, now): self._now = now
    def now(self): return self._now
    def advance(self, s): self._now += timedelta(seconds=s)


def _result(answer):
    return PlannerResult(request_id="req-1", retrieval_trace_id="t",
                         outcome=PlannerOutcome.RETURN, answer=answer)


class Stub:
    def __init__(self, answer=None, raises=None):
        self.answer, self.raises, self.calls = answer, raises, 0
    async def plan(self, ctx):
        self.calls += 1
        if self.raises:
            raise self.raises
        return _result(self.answer)


def _ctx():
    return PlannerContext(request_id="req-1",
                          trigger=UserRequest(request_id="req-1", text="hi"),
                          session_id="s1", limit=10)


async def test_primary_result_is_returned_when_it_succeeds():
    primary, fallback = Stub("primary"), Stub("fallback")
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))))
    assert (await p.plan(_ctx())).answer == "primary"
    assert fallback.calls == 0


async def test_primary_failure_falls_back():
    primary = Stub(raises=RuntimeError("boom"))
    fallback = Stub("fallback")
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))))
    assert (await p.plan(_ctx())).answer == "fallback"


async def test_primary_timeout_falls_back():
    primary = Stub(raises=asyncio.TimeoutError())
    fallback = Stub("fallback")
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))))
    assert (await p.plan(_ctx())).answer == "fallback"


async def test_breaker_opens_after_three_consecutive_failures():
    clock = FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))
    primary = Stub(raises=RuntimeError("boom"))
    fallback = Stub("fallback")
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=clock))
    for _ in range(3):
        await p.plan(_ctx())
    assert primary.calls == 3
    await p.plan(_ctx())          # breaker open — primary must not be tried
    assert primary.calls == 3


async def test_breaker_half_opens_after_the_reset_window():
    clock = FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))
    primary = Stub(raises=RuntimeError("boom"))
    fallback = Stub("fallback")
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=clock, reset_after_s=120.0))
    for _ in range(3):
        await p.plan(_ctx())
    clock.advance(121)
    await p.plan(_ctx())
    assert primary.calls == 4, "breaker should have probed the primary"


def test_a_success_resets_the_failure_count():
    clock = FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))
    breaker = CircuitBreaker(clock=clock)
    breaker.record_failure(); breaker.record_failure()
    breaker.record_success()
    breaker.record_failure(); breaker.record_failure()
    assert breaker.allow() is True, "two failures after a success must not open it"


async def test_fallback_failure_propagates():
    primary = Stub(raises=RuntimeError("primary boom"))
    fallback = Stub(raises=ValueError("fallback boom"))
    p = FallbackPlanner(primary=primary, fallback=fallback,
                        breaker=CircuitBreaker(clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))))
    with pytest.raises(ValueError):
        await p.plan(_ctx())


def test_fallback_planner_satisfies_plannerlike():
    from openrecall_server.agent.planner import PlannerLike
    p = FallbackPlanner(primary=Stub(), fallback=Stub(),
                        breaker=CircuitBreaker(clock=FakeClock(datetime(2026, 9, 7, tzinfo=timezone.utc))))
    assert isinstance(p, PlannerLike)
