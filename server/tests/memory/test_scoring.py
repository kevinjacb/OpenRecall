"""Tests for the Scorer Protocol and its two reference implementations.

A Scorer ranks one candidate against a query. The Retriever owns the
pluggable seam: a Scorer is injected at construction time so future
signals (importance, user preference, session affinity) can replace or
augment semantic similarity without touching the Retriever.
"""
from __future__ import annotations

import math

from openrecall_server.memory.scoring import FixedScorer, Scorer, SimRecencyScorer


def test_scorer_protocol_satisfied_by_reference_impls():
    assert isinstance(SimRecencyScorer(), Scorer)
    assert isinstance(FixedScorer(), Scorer)


def test_sim_recency_scorer_cosine_one_for_identical():
    s = SimRecencyScorer(half_life_s=7 * 24 * 3600)
    v = [1.0, 0.0, 0.0]
    assert math.isclose(s.score(v, v, 0), 1.0, abs_tol=1e-9)


def test_sim_recency_scorer_zero_for_orthogonal():
    s = SimRecencyScorer()
    assert s.score([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], 0) == 0.0


def test_sim_recency_scorer_cosine_zero_for_zero_vector():
    s = SimRecencyScorer()
    assert s.score([0.0, 0.0, 0.0], [1.0, 0.0, 0.0], 0) == 0.0


def test_sim_recency_scorer_half_life_decay():
    s = SimRecencyScorer(half_life_s=100)
    v = [1.0, 0.0, 0.0]
    assert math.isclose(s.score(v, v, 100), 0.5, abs_tol=1e-9)
    assert math.isclose(s.score(v, v, 200), 0.25, abs_tol=1e-9)
    assert math.isclose(s.score(v, v, 0), 1.0, abs_tol=1e-9)


def test_sim_recency_scorer_name_version():
    s = SimRecencyScorer()
    assert s.name == "sim_recency"
    assert s.version == "v1"


def test_fixed_scorer_returns_one():
    s = FixedScorer()
    assert s.score([1.0, 0.0], [0.0, 1.0], 999) == 1.0
    assert s.score([0.0, 0.0], [0.0, 0.0], 0) == 1.0


def test_fixed_scorer_name_version():
    s = FixedScorer()
    assert s.name == "fixed"
    assert s.version == "v1"
