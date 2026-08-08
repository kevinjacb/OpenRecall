"""Tests for the §G MemoryAtom model and its Provenance view."""
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from openrecall_server.memory.atom import MemoryAtom


def test_atom_default_version_fields():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )
    assert atom.extraction_version == "v1"
    assert atom.embedding_model == ""
    assert atom.embedding_version == 0
    assert atom.extractor_prompt_version == "v1"
    assert atom.source_pipeline_version == "transcript"


def test_atom_immutability():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )
    with pytest.raises(ValidationError):
        atom.text = "goodbye"


def test_atom_to_provenance():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        extraction_version="v2", embedding_model="bge-small",
    )
    prov = atom.to_provenance()
    assert prov.session_id == "s1"
    assert prov.source_event_id == "e1"
    assert prov.extraction_version == "v2"
    assert prov.embedding_model == "bge-small"
