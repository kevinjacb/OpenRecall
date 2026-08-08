"""Pluggable retrieval scoring (M2.2).

The Scorer Protocol is the single seam for "how do I rank this candidate
against this query?" The default :class:`SimRecencyScorer` blends cosine
similarity with a recency decay so the most relevant and most recent
atoms float to the top. Future signals (importance, user preference,
session affinity) live as additional :class:`Scorer` impls — the
:class:`~opensapien_server.memory.retrieval.Retriever` is unchanged.
"""
from __future__ import annotations

import math
from typing import Protocol, runtime_checkable

Vector = list[float]


@runtime_checkable
class Scorer(Protocol):
    """Rank one (query, candidate) pair as a non-negative float.

    Higher is better. A score of ``0.0`` excludes the candidate from
    the result set. Implementations must be deterministic for the same
    inputs (no clock, no random) so the same retriever call returns
    the same result.
    """

    name: str
    version: str

    def score(self, query_vec: Vector, atom_vec: Vector, age_s: float) -> float:
        ...


def _cosine(a: Vector, b: Vector) -> float:
    """Cosine similarity; 0.0 if either vector has zero magnitude."""
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


class SimRecencyScorer:
    """Default scorer: cosine similarity * recency decay.

    The decay is exponential with a configurable half-life (default
    7 days). ``age_s = 0`` means no decay, ``age_s = half_life`` means
    0.5, ``age_s = 2*half_life`` means 0.25. The function is monotonic
    decreasing in age, bounded in ``[0, 1]``.
    """

    name = "sim_recency"
    version = "v1"

    def __init__(self, half_life_s: float = 7 * 24 * 3600) -> None:
        if half_life_s <= 0:
            raise ValueError("half_life_s must be positive")
        self._half_life_s = half_life_s

    def score(self, query_vec: Vector, atom_vec: Vector, age_s: float) -> float:
        cos = _cosine(query_vec, atom_vec)
        if cos <= 0.0:
            return 0.0
        if age_s < 0.0:
            age_s = 0.0
        return cos * math.exp(-math.log(2) * age_s / self._half_life_s)


class FixedScorer:
    """Test scorer — returns 1.0 for everything, so test ordering is
    driven by the tiebreaker cascade (created_at desc, atom_id asc)."""

    name = "fixed"
    version = "v1"

    def score(self, query_vec: Vector, atom_vec: Vector, age_s: float) -> float:
        return 1.0
