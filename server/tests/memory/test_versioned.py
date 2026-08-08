"""Tests for memory/versioned.py — stamp_version_metadata.

The function is a binding no-op for v1 atoms: it asserts that an atom
with the default version metadata passes through unchanged, and that
calling it twice produces the same result. Future re-extraction paths
(per the design) will produce NEW atoms that supersede old ones via
``Provenance.supersedes_atom_id``; we never mutate an existing atom.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from opensapien_server.memory.atom import MemoryAtom
from opensapien_server.memory.versioned import stamp_version_metadata


def _atom(atom_id: str = "a1", text: str = "x") -> MemoryAtom:
    return MemoryAtom(
        atom_id=atom_id,
        session_id="s1",
        source_event_id="e1",
        kind="fact",
        text=text,
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )


def test_stamp_version_metadata_no_op_for_default_atom():
    a = _atom()
    before = a.model_dump()
    stamp_version_metadata(a)
    after = a.model_dump()
    assert before == after


def test_stamp_version_metadata_idempotent():
    a = _atom()
    stamp_version_metadata(a)
    snapshot = a.model_dump()
    stamp_version_metadata(a)
    stamp_version_metadata(a)
    assert a.model_dump() == snapshot


def test_stamp_version_metadata_does_not_change_v1_metadata():
    a = _atom()
    stamp_version_metadata(a)
    assert a.extraction_version == "v1"
    assert a.extractor_prompt_version == "v1"
    assert a.source_pipeline_version == "transcript"
    assert a.embedding_model == ""
    assert a.embedding_version == 0
