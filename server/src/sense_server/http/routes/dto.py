"""Transport DTOs for the cognitive read path (N1.1).

The DTOs are the wire shape for the agent + memory HTTP endpoints. They
are deliberately *frozen* and *strict* — ``extra="forbid"`` so any
future field added to the server is rejected by the client until the
client is updated. The ``schema_version`` field is the binding
forward-compat handle: a client that sees a ``schema_version`` it
doesn't understand can refuse the response cleanly.

INV-8: these DTOs are the only place the wire shape is defined. The
Planner domain types and the HTTP layer never share a class.
"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Transport constants — keep magic numbers here, not at the call site.
SCHEMA_VERSION = "v1"
MAX_ATOM_CHIP_TEXT_LEN = 240   # chips on the chat screen cap text length


# --- /agent -----------------------------------------------------------------


class AtomChipDTO(BaseModel):
    """A compact, tappable representation of one cited atom.

    The chat screen renders a row of these under each answer; tapping
    one navigates to the AtomDetail screen. Text is truncated to
    :data:`MAX_ATOM_CHIP_TEXT_LEN` characters + an ellipsis at the
    mapper layer (N1.2) so the wire shape carries the display-ready
    value, not the full text.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    atom_id: str
    session_id: str
    kind: str
    text: str
    created_at: datetime
    start_ms: int
    score: float


class AgentRequestDTO(BaseModel):
    """Inbound payload for ``POST /agent``.

    The session_id is optional: a request with ``session_id=None``
    triggers a global search across all sessions (per the Retriever's
    session_id filter).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    session_id: str | None = None
    text: str = Field(min_length=1, max_length=4096)
    limit: int = Field(default=10, ge=1, le=50)


class AgentResponseDTO(BaseModel):
    """Outbound payload for ``POST /agent``.

    The outcome is one of three strings:

      * ``"return"`` — the agent answered (see ``answer`` + ``atoms``).
      * ``"return_with_uncertainty"`` — the agent answered but its
        confidence was below the autonomous threshold.
      * ``"refuse"`` — the agent refused (see ``refusal_reason``).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    request_id: str
    retrieval_trace_id: str
    audit_id: str
    outcome: Literal["return", "return_with_uncertainty", "refuse"]
    answer: str | None = None
    atoms: list[AtomChipDTO] = Field(default_factory=list)
    confidence: float | None = None
    confidence_band: Literal["low", "medium", "high"] | None = None
    refusal_reason: str | None = None
    payload: dict | None = None


# --- /memory ----------------------------------------------------------------


class MemoryAtomDTO(BaseModel):
    """Full memory atom — the wire shape for the memory browsing screen."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    atom_id: str
    session_id: str
    kind: str
    text: str
    created_at: datetime
    start_ms: int
    source_event_id: str
    source_modality: str
    extraction_version: str
    embedding_model: str
    extractor_prompt_version: str


class MemorySearchResponseDTO(BaseModel):
    """Outbound payload for ``GET /memory``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    request_id: str
    retrieval_trace_id: str
    audit_id: str
    query: str
    session_id: str | None = None
    atoms: list[MemoryAtomDTO] = Field(default_factory=list)
    returned_count: int = 0
    top_score: float = 0.0
    retrieval_latency_ms: int = 0


class SessionMemoryResponseDTO(BaseModel):
    """Outbound payload for ``GET /sessions/{id}/memory``."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    session_id: str
    atoms: list[MemoryAtomDTO] = Field(default_factory=list)
    returned_count: int = 0


# --- error envelope ---------------------------------------------------------


class ErrorEnvelopeDTO(BaseModel):
    """The single error wire shape.

    All error responses use this envelope so the client can switch on
    ``code`` uniformly. Domain outcomes (refusal, validation failure)
    are NOT errors — they are successful HTTP responses with
    ``outcome: "refuse"`` on the AgentResponseDTO.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["v1"] = "v1"
    code: Literal[
        "bad_request",
        "unauthorized",
        "not_found",
        "rate_limited",
        "internal_error",
    ]
    message: str
    request_id: str | None = None
