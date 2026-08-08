"""Tests for the IdGenerator Protocol and its two reference implementations.

The IdGenerator is the single seam for "give me a fresh id" — production
uses :class:`UuidIdGenerator`; tests inject :class:`DeterministicIdGenerator`
to make request_id, retrieval_trace_id, and audit_id reproducible.
"""
from __future__ import annotations

import re

from openrecall_server.contracts.id_generator import DeterministicIdGenerator, UuidIdGenerator

_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def test_uuid_generator_returns_uuid_v4_string():
    g = UuidIdGenerator()
    ids = [g.new() for _ in range(8)]
    assert all(_UUID_RE.match(i) for i in ids)
    # all unique
    assert len(set(ids)) == 8


def test_uuid_generator_unique():
    g = UuidIdGenerator()
    assert g.new() != g.new()


def test_deterministic_generator_sequential_prefix():
    g = DeterministicIdGenerator()
    assert g.new() == "trace-0001"
    assert g.new() == "trace-0002"
    assert g.new() == "trace-0003"


def test_deterministic_generator_custom_prefix():
    g = DeterministicIdGenerator(prefix="audit")
    assert g.new() == "audit-0001"
    assert g.new() == "audit-0002"


def test_deterministic_generator_pad_width():
    g = DeterministicIdGenerator(pad=6)
    assert g.new() == "trace-000001"
