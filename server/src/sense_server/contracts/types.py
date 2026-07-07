"""Shared contract types exchanged across module boundaries.

These types are deliberately minimal: they're the wire/in-process shapes other
modules depend on, not the rich domain models that live next to the code that
owns them. Anything here is stable across the server; anything local lives in
its own module.

All types are :class:`~pydantic.BaseModel` with ``frozen=True`` so they can
be safely shared across threads and asserted on by value in tests.
"""
from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# Memory atom provenance (M1.1)
# =============================================================================


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


# =============================================================================
# Agent pipeline (M2.4 onwards)
# =============================================================================


# Confidence is a constrained float in [0, 1]. The LLM reports one; the
# Validator enforces it; the Guardrails uses it to gate the action.
Confidence = float


class AgentActionKind(str, Enum):
    """Two outcomes the agent can produce in this slice.

    P2-answers covers only ``ANSWER`` and ``NO_MEMORY``. P2-commands will
    add ``ISSUE_COMMAND`` and ``REFUSE`` (currently expressed via
    :class:`RejectionReason` on the validated action).
    """

    ANSWER = "answer"
    NO_MEMORY = "no_memory"


class AgentAction(BaseModel):
    """The parsed action the LLM intends to take.

    ``atom_ids`` is the set of memory atoms the LLM is citing in support of
    ``text``. The Validator enforces that every cited id was actually
    retrieved; anything else is a hallucination and gets refused.
    """

    model_config = ConfigDict(frozen=True)
    kind: AgentActionKind
    text: str
    atom_ids: tuple[str, ...] = Field(default_factory=tuple)
    confidence: float = Field(ge=0.0, le=1.0)


class LLMResult(BaseModel):
    """The raw LLM response, plus the best-effort parsed :class:`AgentAction`.

    ``parse_error`` is ``None`` iff ``parsed`` is also ``None`` (a successful
    parse). Callers should treat ``parsed is None`` as the failure signal —
    never trust the raw text without a parse.
    """

    model_config = ConfigDict(frozen=True)
    raw: str
    parsed: AgentAction | None = None
    parse_error: str | None = None


class RejectionReason(str, Enum):
    """Why a Validator or Guardrail refused the action.

    Every refusal is audited (not silently dropped). Adding a new reason is
    a one-line change here; downstream consumers switch on the value.
    """

    INVALID_JSON = "invalid_json"
    SCHEMA_MISMATCH = "schema_mismatch"
    NO_ATOM_CITED = "no_atom_cited"
    CITED_ATOM_NOT_RETRIEVED = "cited_atom_not_retrieved"
    CONFIDENCE_OUT_OF_RANGE = "confidence_out_of_range"
    RATE_LIMIT = "rate_limit"
    NOT_AUTONOMOUS = "not_autonomous"
    NO_SUPPORTING_MEMORY = "no_supporting_memory"
    UNKNOWN = "unknown"


class ValidatorContext(BaseModel):
    """Inputs the Validator needs to evaluate one LLM result.

    ``retrieved_atom_ids`` is the set of atom ids the Retriever returned;
    the Validator rejects any cited id outside this set. The threshold is
    the recency/score floor above which "no_memory" is treated as honest
    "I don't know" rather than a refusal.
    """

    model_config = ConfigDict(frozen=True)
    retrieved_atom_ids: tuple[str, ...] = Field(default_factory=tuple)
    no_memory_top_score_threshold: float = 0.3


class ValidatedAction(BaseModel):
    """The output of the Validator.

    ``rejection is None`` means ``action`` was accepted; otherwise
    ``action`` is the parsed result (or a stub) and ``rejection`` says
    why it was rejected. A non-None ``rejection`` is treated as a refusal
    by every downstream consumer.
    """

    model_config = ConfigDict(frozen=True)
    action: AgentAction
    rejection: RejectionReason | None = None


# =============================================================================
# Retrieval (used by M3.2)
# =============================================================================


class RetrieverContext(BaseModel):
    """Inputs the Retriever needs to answer one query."""

    model_config = ConfigDict(frozen=True)
    query_text: str
    limit: int = 10
    session_id: str | None = None  # None = global retrieval


class ScoredAtom(BaseModel):
    """One atom as the Retriever scored it.

    Carries the score alongside the atom's public fields so callers can
    rank, filter, or audit without touching the underlying atom storage.
    """

    model_config = ConfigDict(frozen=True)
    atom_id: str
    session_id: str
    kind: str
    text: str
    created_at: datetime
    start_ms: int
    score: float
    source_event_id: str = ""
    provenance: Provenance | None = None


class RetrievedContext(BaseModel):
    """The full output of one retrieval.

    Carries metadata (strategy, index, scores) so audit, observability,
    and the Agent LLM prompt can reason about the result.
    """

    model_config = ConfigDict(frozen=True)
    atoms: tuple[ScoredAtom, ...] = Field(default_factory=tuple)
    retrieval_strategy: str = "sim_recency"
    scorer_version: str = "v1"
    index_name: str = "in_memory"
    index_version: str = "v1"
    top_score: float = float("-inf")
    lowest_score: float = float("-inf")
    returned_count: int = 0
    retrieval_latency_ms: int = 0
    candidate_count: int = 0
    session_filter: str | None = None
    retrieval_trace_id: str = ""
