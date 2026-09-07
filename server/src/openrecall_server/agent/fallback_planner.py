"""Keep answering when the primary reasoning layer is down (spec §5.7).

`PlannerLike` has one method, so a wrapper that tries one implementation and
falls back to another is itself a PlannerLike — which is why rolling back to
the pre-Hermes planner is a config value and not a code change.
"""
from __future__ import annotations

import asyncio
import logging

from ..contracts.types import PlannerContext, PlannerResult

log = logging.getLogger(__name__)


class CircuitBreaker:
    """Stop hammering a primary that is consistently failing.

    Consecutive failures only: one success resets the count, because an
    intermittent failure is not an outage.
    """

    def __init__(self, *, clock, threshold: int = 3,
                 reset_after_s: float = 120.0) -> None:
        self._clock = clock
        self._threshold = threshold
        self._reset_after_s = reset_after_s
        self._failures = 0
        self._opened_at = None
        # Set only while a half-open probe is outstanding (between the
        # `allow()` call that grants it and the matching record_*()).
        # Tracked separately from `_failures` so a probe that fails can
        # reopen the breaker immediately, rather than needing `_threshold`
        # more consecutive failures to re-trip it.
        self._half_open = False

    @property
    def is_open(self) -> bool:
        return self._opened_at is not None

    def allow(self) -> bool:
        """True if the primary should be tried (closed, or a half-open probe)."""
        if self._opened_at is None:
            return True
        elapsed = (self._clock.now() - self._opened_at).total_seconds()
        if elapsed >= self._reset_after_s:
            self._opened_at = None          # half-open: allow one probe
            self._failures = 0
            self._half_open = True
            return True
        return False

    def record_success(self) -> None:
        self._failures = 0
        self._opened_at = None
        self._half_open = False

    def record_failure(self) -> None:
        if self._half_open:
            # The probe failed: the primary is still down. Reopen at once —
            # do not make this outage wait for `_threshold` more failures.
            self._half_open = False
            self._opened_at = self._clock.now()
            return
        self._failures += 1
        if self._failures >= self._threshold:
            self._opened_at = self._clock.now()


class FallbackPlanner:
    """Implements PlannerLike. Tries `primary`; on failure serves `fallback`."""

    def __init__(self, *, primary, fallback, breaker: CircuitBreaker,
                 metrics=None) -> None:
        self._primary = primary
        self._fallback = fallback
        self._breaker = breaker
        # Forward plumbing: not read yet. Task 4 wires the fallback-engaged /
        # breaker-open counters once the metric names are decided alongside
        # the rest of the Hermes rollout's observability surface.
        self._metrics = metrics

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        if self._breaker.allow():
            try:
                result = await self._primary.plan(ctx)
            except (asyncio.TimeoutError, Exception) as exc:   # noqa: B014
                self._breaker.record_failure()
                log.warning(
                    "primary_planner_failed request_id=%s breaker_open=%s",
                    ctx.request_id, self._breaker.is_open, exc_info=exc,
                )
            else:
                self._breaker.record_success()
                return result
        else:
            log.info("primary_planner_skipped_breaker_open request_id=%s",
                     ctx.request_id)
        return await self._fallback.plan(ctx)
