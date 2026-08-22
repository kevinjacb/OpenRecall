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
    session_id: str | None  # None for captures that couldn't be mapped to a session (e.g. vision snapshots)
    source_event_id: str  # provenance back into the §F event log
    kind: str
    text: str
    created_at: datetime  # when the *server* learned this — extraction wall clock
    start_ms: int  # offset within the session timeline (from the source event)
    # When the thing being remembered was actually *said* — the source
    # capture event's wall clock (spec D6). Extraction runs in batch after
    # the fact, so `created_at` is when the server learned something: last
    # night's session extracted this morning would report as "added today"
    # and every atom in it would share one timestamp. Every ordering and
    # counting surface the app sees uses `occurred_at`; `created_at` stays
    # on the wire for debugging.
    #
    # Nullable for atoms written before this field existed (and by callers
    # that have no source event to date). Read through `timeline_at`, which
    # falls back to `created_at`, rather than touching this directly.
    occurred_at: datetime | None = None
    extraction_version: str = "v1"
    embedding_model: str = ""
    embedding_version: int = 0
    extractor_prompt_version: str = "v1"
    source_pipeline_version: str = "transcript"
    speaker: str | None = None  # majority speaker UUID for the extraction window
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None  # "confirmed" | "tentative" | "none"

    @property
    def timeline_at(self) -> datetime:
        """Conversation time for this atom — ``occurred_at`` or ``created_at``.

        The single accessor for "when did this happen", so the fallback for
        pre-``occurred_at`` rows lives in one place instead of at every
        ordering, filtering and counting call site.
        """
        return self.occurred_at or self.created_at

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
