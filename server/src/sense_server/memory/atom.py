"""§G memory atom — a structured memory derived from capture events.

Atoms are the queryable layer built on top of the immutable §F event log. Each atom
keeps provenance (``source_event_id``) back to the event it came from and a
denormalised ``start_ms`` so atoms place themselves on the session timeline without
a join. ``kind`` is free-form (the extraction model decides categories like
"fact" / "task" / "preference"), so it is a plain string rather than an enum.
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
