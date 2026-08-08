"""Agent-layer metrics recorder.

The in-memory recorder is the binding implementation in this slice:
it enforces the canonical-name contract, stores counters and histograms,
and renders Prometheus text for ``GET /metrics``. A Prometheus client
backend can replace it without changing any caller.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable

from ..contracts.metrics import KNOWN_METRICS, Metrics


@dataclass(frozen=True)
class HistogramSnapshot:
    """Frozen view of one histogram — sum, count, min, max."""

    sum: float
    count: int
    min: float
    max: float


class InMemoryMetricsRecorder:
    """Process-local recorder for canonical metrics.

    Any ``observe`` or ``increment`` call with a name not in
    :data:`KNOWN_METRICS` raises :class:`ValueError`. The recorder is
    safe to call from multiple coroutines on the same loop (no locks
    needed — Python dict operations are atomic under the GIL).
    """

    def __init__(self) -> None:
        self._counters: dict[tuple[str, frozenset[tuple[str, str]]], int] = {}
        self._histograms: dict[str, list[float]] = {}

    # --- internal ---------------------------------------------------------

    @staticmethod
    def _check(name: str) -> None:
        if name not in KNOWN_METRICS:
            raise ValueError(
                f"unknown metric: {name!r}; add it to contracts.metrics.Metrics"
            )

    @staticmethod
    def _tags_key(tags: dict[str, str] | None) -> frozenset[tuple[str, str]]:
        if not tags:
            return frozenset()
        return frozenset(tags.items())

    # --- mutation ---------------------------------------------------------

    def observe(
        self, name: str, value: float, tags: dict[str, str] | None = None
    ) -> None:
        self._check(name)
        # Tags are allowed on histograms but the recorder currently
        # treats each name as a single series (per the brief's simple
        # implementation). Tags are accepted for API symmetry.
        del tags
        self._histograms.setdefault(name, []).append(float(value))

    def increment(
        self, name: str, tags: dict[str, str] | None = None
    ) -> None:
        self._check(name)
        key = (name, self._tags_key(tags))
        self._counters[key] = self._counters.get(key, 0) + 1

    # --- query ------------------------------------------------------------

    def counter(
        self, name: str, tags: dict[str, str] | None = None
    ) -> int:
        """Return the current counter value for the given name+tags (0 if absent)."""
        return self._counters.get((name, self._tags_key(tags)), 0)

    def histogram(self, name: str) -> HistogramSnapshot:
        """Return a frozen snapshot of the named histogram (zeros if absent)."""
        values = self._histograms.get(name, [])
        if not values:
            return HistogramSnapshot(sum=0.0, count=0, min=0.0, max=0.0)
        return HistogramSnapshot(
            sum=sum(values),
            count=len(values),
            min=min(values),
            max=max(values),
        )

    # --- render -----------------------------------------------------------

    def render(self) -> str:
        """Render the current state as Prometheus 0.0.4 text format.

        Counters and histograms are emitted as separate families. A counter
        with labels is emitted once per unique label set.
        """
        lines: list[str] = []
        # Counters, grouped by name.
        for name in sorted({n for n, _ in self._counters}):
            lines.append(f"# TYPE {name} counter")
            for (n, label_set), v in sorted(self._counters.items()):
                if n != name:
                    continue
                if label_set:
                    labels = ",".join(f'{k}="{val}"' for k, val in sorted(label_set))
                    lines.append(f"{n}{{{labels}}} {v}")
                else:
                    lines.append(f"{n} {v}")
        # Histograms, simple sum/count form.
        for name in sorted(self._histograms):
            values = self._histograms[name]
            lines.append(f"# TYPE {name} histogram")
            lines.append(f"{name}_sum {sum(values)}")
            lines.append(f"{name}_count {len(values)}")
        if not lines:
            return ""
        return "\n".join(lines) + "\n"

    # --- bulk access for tests -------------------------------------------

    def _all_counter_names(self) -> Iterable[str]:
        return sorted({n for n, _ in self._counters})
