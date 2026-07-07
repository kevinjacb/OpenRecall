"""§G memory atom — a structured memory derived from capture events.

Atoms are the queryable layer built on top of the immutable §F event log. Each atom
keeps provenance (``source_event_id``) back to the event it came from and a
denormalised ``start_ms`` so atoms place themselves on the session timeline without
a join. ``kind`` is free-form (the extraction model decides categories like
"fact" / "task" / "preference"), so it is a plain string rather than an enum.

The five ``*_version`` fields are defaulted so older call sites that only carry
the original seven fields keep working — newer ingestion paths override them
with the actual pipeline versions used.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MemoryAtom(BaseModel):
    model_config = ConfigDict(frozen=True)  # immutable once created

    atom_id: str  # idempotency key
    session_id: str
    source_event_id: str  # provenance back into the §F event log
    kind: str
    text: str
    created_at: datetime
    start_ms: int  # offset within the session timeline (from the source event)
    extraction_version: str = "v1"
    embedding_model: str = ""
    embedding_version: int = 0
    extractor_prompt_version: str = "v1"
    source_pipeline_version: str = "transcript"

    def to_provenance(self) -> "Provenance":
        """Return a structured ``Provenance`` view of this atom's origin.

        ``source_modality`` is derived from ``source_pipeline_version`` (only
        the ``"vision"`` pipeline maps to ``"vision"``; everything else is
        reported as ``"transcript"`` until more modalities are wired up).
        """
        from ..contracts.types import Provenance

        return Provenance(
            session_id=self.session_id,
            source_event_id=self.source_event_id,
            source_modality=("vision" if self.source_pipeline_version == "vision" else "transcript"),
            extraction_version=self.extraction_version,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            extractor_prompt_version=self.extractor_prompt_version,
            source_pipeline_version=self.source_pipeline_version,
            created_at=self.created_at,
        )
