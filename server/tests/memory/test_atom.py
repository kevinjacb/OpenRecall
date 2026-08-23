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


def test_atom_to_provenance_sessionless():
    """A sessionless atom (vision snapshot that couldn't be mapped to a
    session) must produce a valid Provenance with ``session_id=None`` — not
    raise. ``Provenance.session_id`` is ``str | None`` so the read/wire path
    (retrieval -> ScoredAtom -> DTO) can carry sessionless atoms.
    """
    atom = MemoryAtom(
        atom_id="scene:abc", session_id=None, source_event_id="blob:abc",
        kind="scene", text="a cat on the desk",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0, source_pipeline_version="vision",
    )
    prov = atom.to_provenance()
    assert prov.session_id is None
    assert prov.source_modality == "vision"
    assert prov.source_pipeline_version == "vision"
