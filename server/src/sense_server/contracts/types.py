"""Shared contract types exchanged across module boundaries.

These types are deliberately minimal: they're the wire/in-process shapes other
modules depend on, not the rich domain models that live next to the code that
owns them. Anything here is stable across the server; anything local lives in
its own module.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict


class Provenance(BaseModel):
    """Structured origin metadata for a MemoryAtom.

    Produced via :meth:`sense_server.memory.atom.MemoryAtom.to_provenance`.
    Frozen so downstream code can rely on it as an immutable record of "where
    this atom came from and which pipeline versions built it."
    """

    model_config = ConfigDict(frozen=True)
    session_id: str
    source_event_id: str
    source_modality: Literal["transcript", "vision", "ocr", "sensor", "bluetooth"]
    extraction_version: str
    embedding_model: str
    embedding_version: int
    extractor_prompt_version: str
    source_pipeline_version: str
    created_at: datetime
    supersedes_atom_id: str | None = None
    superseded_by_atom_id: str | None = None
