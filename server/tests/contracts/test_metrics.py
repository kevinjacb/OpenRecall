"""Tests for canonical metric names and the in-memory recorder.

The recorder is the binding implementation in this slice; production
wires it into the gateway so ``GET /metrics`` renders Prometheus text.
"""
from __future__ import annotations

import pytest

from opensapien_server.agent.metrics import InMemoryMetricsRecorder
from opensapien_server.contracts.metrics import KNOWN_METRICS, Metrics


def test_metrics_known_set_contains_required_names():
    assert Metrics.PLANNER_LATENCY_MS in KNOWN_METRICS
    assert Metrics.RETRIEVAL_LATENCY_MS in KNOWN_METRICS
    assert Metrics.RETRIEVAL_HITS_TOTAL in KNOWN_METRICS
    assert Metrics.REFUSALS_TOTAL in KNOWN_METRICS
    assert Metrics.INDEXING_FAILURES_TOTAL in KNOWN_METRICS
    assert Metrics.AUDIT_RECORDS_TOTAL in KNOWN_METRICS


def test_recorder_rejects_unknown_metric_name_on_observe():
    r = InMemoryMetricsRecorder()
    with pytest.raises(ValueError, match="not_canonical"):
        r.observe("not_canonical", 1.0)


def test_recorder_rejects_unknown_metric_name_on_increment():
    r = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        r.increment("bogus_total")


def test_recorder_increment_counts():
    r = InMemoryMetricsRecorder()
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL)
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL)
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL, tags={"session_id": "s1"})
    assert r.counter(Metrics.RETRIEVAL_HITS_TOTAL) == 2
    assert r.counter(Metrics.RETRIEVAL_HITS_TOTAL, tags={"session_id": "s1"}) == 1


def test_recorder_observe_records_histogram():
    r = InMemoryMetricsRecorder()
    for v in (1.0, 2.0, 3.0, 4.0, 5.0):
        r.observe(Metrics.PLANNER_LATENCY_MS, v)
    hist = r.histogram(Metrics.PLANNER_LATENCY_MS)
    assert hist.count == 5
    assert hist.sum == 15.0
    assert hist.min == 1.0
    assert hist.max == 5.0


def test_recorder_render_prometheus_text():
    r = InMemoryMetricsRecorder()
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL)
    r.observe(Metrics.PLANNER_LATENCY_MS, 42.0)
    text = r.render()
    assert "# TYPE retrieval_hits_total counter" in text
    assert "retrieval_hits_total 1" in text
    assert "# TYPE planner_latency_ms histogram" in text
    assert "planner_latency_ms_count 1" in text


def test_recorder_render_with_labels():
    r = InMemoryMetricsRecorder()
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL, tags={"session_id": "s1"})
    text = r.render()
    assert 'retrieval_hits_total{session_id="s1"} 1' in text
