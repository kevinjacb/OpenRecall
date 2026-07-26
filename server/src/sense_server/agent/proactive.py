"""P3 proactive trigger engine.

The :class:`ProactiveTriggerEngine` is a listener on the extraction
worker. After every successful extraction it builds a
:class:`Proactive` trigger and invokes the planner; a ``RETURN``
outcome is forwarded to the phone via a :class:`WsSender`. The engine
is best-effort: a 2-second plan timeout (configurable), every drop
counted.

The engine never re-raises. A planner crash, a timeout, a
``ws_sender`` failure — all are caught, logged, and counted. The
proactive ``ISSUE_COMMAND`` prohibition is enforced inside the planner
itself (Commit 2); the engine just observes the result and acts on
``RETURN`` / ``REFUSE``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Mapping, Protocol, runtime_checkable

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.metrics import Metrics
from ..contracts.types import PlannerContext, PlannerOutcome, Proactive
from ..memory.extraction_worker import SessionCompletion
from .metrics import InMemoryMetricsRecorder
from .planner import Planner

log = logging.getLogger(__name__)


DEFAULT_PLAN_TIMEOUT_S = 8.0
ENV_PROACTIVE_PLAN_TIMEOUT_S = "SENSE_PROACTIVE_PLAN_TIMEOUT_S"


def plan_timeout_from_env(
    env: Mapping[str, str], default: float = DEFAULT_PLAN_TIMEOUT_S
) -> float:
    """Read ``SENSE_PROACTIVE_PLAN_TIMEOUT_S``, validating it is > 0.

    The previous hard-coded 2.0s was an SLA, not a model-grounded budget:
    a cold local 7B model loads in ~2.1s, so every cold proactive call
    timed out before generating (``proactive_plan_timeout``). 8.0s fits a
    cold load + generation on modest hardware while still bounding a
    stuck planner. Operators can lower/raise it per model without a
    code change.
    """
    raw = env.get(ENV_PROACTIVE_PLAN_TIMEOUT_S)
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError as e:
        raise ValueError(
            f"{ENV_PROACTIVE_PLAN_TIMEOUT_S}={raw!r} is not a valid float"
        ) from e
    if val <= 0:
        raise ValueError(f"{ENV_PROACTIVE_PLAN_TIMEOUT_S}={val} must be > 0")
    return val


@runtime_checkable
class WsSender(Protocol):
    """The seam between the engine and the gateway's WS frame.

    The gateway's :class:`GatewayCore` implements this without
    inheriting from it (Protocol is structural). The engine only
    knows it can call ``send_proactive`` and await the result.
    """

    async def send_proactive(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        atoms: tuple[str, ...],
    ) -> None: ...


class ProactiveTriggerEngine:
    """Listens for :class:`SessionCompletion` and fires a Proactive plan call.

    Lifecycle: wire in ``run_gateway.py`` as a listener on the
    :class:`ExtractionWorker`. The engine holds no per-session state;
    every completion is a fresh call.

    The engine's ``ws_sender`` is bound at construction; the gateway
    rebinds it per-connection via :meth:`set_ws_sender` so the
    per-session :class:`GatewayCore` instance becomes the sender.
    """

    def __init__(
        self,
        planner: Planner,
        ws_sender: WsSender,
        clock: Clock,
        metrics: InMemoryMetricsRecorder,
        ids: IdGenerator,
        plan_timeout_s: float = 2.0,
    ) -> None:
        self._planner = planner
        self._ws_sender = ws_sender
        self._clock = clock
        self._metrics = metrics
        self._ids = ids
        self._plan_timeout_s = plan_timeout_s

    def set_ws_sender(self, ws_sender: WsSender) -> None:
        """Rebind the ws_sender. Used by the gateway to bind the
        per-connection core after the engine is constructed."""
        self._ws_sender = ws_sender

    async def on_session_completion(self, completion: SessionCompletion) -> None:
        """One listener call. Best-effort; never re-raises."""
        try:
            ctx = PlannerContext(
                request_id=self._ids.new(),
                trigger=Proactive(
                    request_id=self._ids.new(),
                    event_id=f"event_seq_{completion.event_id_range[1]}",
                    transcript="",  # v1: planner retrieves from session memory
                ),
                session_id=completion.session_id,
                limit=10,
            )
            log.info(
                "proactive_trigger session=%s seq_range=%s",
                completion.session_id, completion.event_id_range,
            )
            try:
                result = await asyncio.wait_for(
                    self._planner.plan(ctx), timeout=self._plan_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "proactive_plan_timeout",
                    extra={
                        "session_id": completion.session_id,
                        "timeout_s": self._plan_timeout_s,
                    },
                )
                self._metrics.increment(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL)
                return
            except Exception:
                log.exception(
                    "proactive_plan_failed",
                    extra={"session_id": completion.session_id},
                )
                self._metrics.increment(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL)
                return

            log.info(
                "proactive_plan_result session=%s outcome=%s atoms=%s "
                "answer=%r",
                completion.session_id, result.outcome, result.atom_ids,
                (result.answer or "")[:200],
            )
            if result.outcome in (
                PlannerOutcome.RETURN, PlannerOutcome.RETURN_WITH_UNCERTAINTY,
            ):
                try:
                    await self._ws_sender.send_proactive(
                        session_id=completion.session_id,
                        request_id=result.request_id,
                        text=result.answer or "",
                        atoms=result.atom_ids,
                    )
                    self._metrics.increment(Metrics.PROACTIVE_DELIVERED_TOTAL)
                    log.info(
                        "proactive_delivered session=%s", completion.session_id,
                    )
                except Exception:
                    log.exception(
                        "proactive_send_failed",
                        extra={"session_id": completion.session_id},
                    )
                    self._metrics.increment(Metrics.PROACTIVE_SEND_FAILURE_TOTAL)
                return

            # REFUSE or any other outcome: drop with a counter.
            self._metrics.increment(Metrics.PROACTIVE_REFUSED_TOTAL)
            log.info(
                "proactive_refused session=%s outcome=%s", completion.session_id,
                result.outcome,
            )
        except Exception:
            # Defensive: catch any unexpected error from the listener
            # signature or argument validation. Never re-raise; the
            # extraction worker must not see listener exceptions.
            log.exception(
                "proactive_engine_unexpected",
                extra={"session_id": completion.session_id},
            )
