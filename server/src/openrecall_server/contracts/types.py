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
from typing import Annotated, Any, Literal, Protocol, Union, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field


# =============================================================================
# Memory atom provenance (M1.1)
# =============================================================================


class Provenance(BaseModel):
    """Structured origin metadata for a MemoryAtom.

    Produced via :meth:`openrecall_server.memory.atom.MemoryAtom.to_provenance`.
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
    """Three outcomes the agent can produce.

    P2-answers covers ``ANSWER`` and ``NO_MEMORY``. P2-commands adds
    ``ISSUE_COMMAND`` for autonomous device actions. ``REFUSE`` is
    expressed via :class:`RejectionReason` on the validated action
    (not a separate kind — the action parser always produces a
    real :class:`AgentAction`, and the Validator flags it as
    rejected rather than discarding it).
    """

    ANSWER = "answer"
    NO_MEMORY = "no_memory"
    ISSUE_COMMAND = "issue_command"


class IssueCommandPayload(BaseModel):
    """The LLM's intent to issue a device command.

    Carries the LLM's parsed intent: the command type, its parameters
    (validated by the CommandValidator against per-type schemas),
    and the user's idempotency key (re-issuing the same key returns
    the same command_id). The Planner + Dispatcher turn this into
    a persisted :class:`~openrecall_server.commands.model.Command`.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")
    command_type: Literal[
        "capture_photo", "record_video", "start_audio",
        "stop_audio", "request_buffer",
    ]
    params: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str
    # LLM-reported confidence. The Guardrails pass applies the
    # confidence threshold; commands above ``autonomous`` execute
    # without confirmation, commands below may be refused.
    confidence: float = Field(ge=0.0, le=1.0)


class AgentAction(BaseModel):
    """The parsed action the LLM intends to take.

    For ``ANSWER`` and ``NO_MEMORY`` kinds, ``atom_ids`` carries the
    memory atoms cited in support of ``text``. For ``ISSUE_COMMAND``,
    ``text`` is empty (the LLM returns a structured command, not a
    sentence) and ``command`` carries the parsed payload.
    """

    model_config = ConfigDict(frozen=True)
    kind: AgentActionKind
    text: str = ""
    atom_ids: tuple[str, ...] = Field(default_factory=tuple)
    confidence: float = Field(ge=0.0, le=1.0)
    command: IssueCommandPayload | None = None


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
    # P2-commands: an issue_command reply reached the validator
    # without a parsed IssueCommandPayload. The parser is the
    # primary gate (it raises a parse error if the field is
    # missing), but a constructed AgentAction or a future parser
    # regression must not slip through. The generic validator
    # surfaces this as MISSING_COMMAND_PAYLOAD so the planner's
    # audit log has a clear reason; the StrictCommandValidator
    # never sees such an action.
    MISSING_COMMAND_PAYLOAD = "missing_command_payload"
    # P3: a Proactive trigger is FORBIDDEN from issuing device commands.
    # Enforced inside Planner.plan before _dispatch_command.
    PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND = "proactive_trigger_cannot_issue_command"


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


class Prompt(BaseModel):
    """The LLM prompt built by :class:`ContextBuilder` (H1).

    The ``system_prompt_version`` and ``context_builder_version`` are
    stamped here so audit + replay can cite the exact prompt shape
    that produced a given action.
    """

    model_config = ConfigDict(frozen=True)
    system: str
    user: str
    system_prompt_version: str = "v1"
    context_builder_version: str = "v1"


class GuardOutcome(str, Enum):
    """What the Guardrails decided to do with a validated action."""

    RETURN = "return"                              # autonomous
    RETURN_WITH_UNCERTAINTY = "return_with_uncertainty"  # below autonomous threshold
    REFUSE = "refuse"                              # explicit refusal


class GuardedAction(BaseModel):
    """The output of :class:`Guardrails`. Either an action to dispatch
    or a refusal with a reason."""

    model_config = ConfigDict(frozen=True)
    outcome: GuardOutcome
    action: AgentAction
    refusal_reason: RejectionReason | None = None
    refusal_message: str | None = None


class CapabilitySet(BaseModel):
    """Device-advertised capabilities (binding for the P3-commands slice).

    P2-answers does not issue commands, so all flags are advisory here;
    the Planner still inspects them so a future P3 layer doesn't
    surprise this slice.
    """

    model_config = ConfigDict(frozen=True)
    camera: bool = False
    microphone: bool = True
    retrospective_buffer: bool = True
    display: bool = False
    speaker: bool = False


class DeviceResourceStatus(BaseModel):
    """Device-side resource snapshot."""

    model_config = ConfigDict(frozen=True)
    battery_pct: float = 1.0
    storage_free_bytes: int = 1 << 30
    camera_available: bool = False
    microphone_available: bool = True
    recording: bool = False
    relay_connected: bool = True


@runtime_checkable
class CapabilityProvider(Protocol):
    """Single seam for "what can the device do right now?"."""

    def capabilities(self) -> CapabilitySet: ...
    def resources(self) -> DeviceResourceStatus: ...


# =============================================================================
# Planner (N3.2 / INV-9)
# =============================================================================


class PlannerOutcome(str, Enum):
    """The terminal outcome of one Planner run.

    The Plan is the only place this enum is defined; the mapper (N1.2)
    translates it to the wire-level ``outcome`` string on
    :class:`~openrecall_server.http.routes.dto.AgentResponseDTO`.

    P2-answers: RETURN, RETURN_WITH_UNCERTAINTY, REFUSE.
    P2-commands: adds ISSUE_COMMAND for autonomous device actions.
    """

    RETURN = "return"                          # autonomous answer
    RETURN_WITH_UNCERTAINTY = "return_with_uncertainty"  # answered, low confidence
    REFUSE = "refuse"                          # explicit refusal
    ISSUE_COMMAND = "issue_command"            # autonomous command dispatch


class UserRequest(BaseModel):
    """The user asked a question via ``POST /agent``.

    ``request_id`` is the inbound request's id, preserved so the audit
    log can correlate the trigger with the result. P3 (proactive
    trigger) introduces :class:`Proactive` as the alternative
    source — see :class:`Trigger`.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["user_request"] = "user_request"
    request_id: str
    text: str


class Proactive(BaseModel):
    """The server-initiated trigger (P3 proactive).

    A session just finished extracting new atoms, and the planner is
    being asked to look for something worth surfacing without the
    user having asked a question. ``event_id`` identifies the
    transcript event that triggered the extraction; ``transcript`` is
    the raw text (v1 sends an empty string — the planner retrieves
    from session memory; capturing the exact transcript is a
    follow-up). A Proactive trigger is FORBIDDEN from issuing device
    commands — enforced inside :meth:`Planner.plan` (not in
    :class:`CommandValidator` or :class:`CommandGuardrails`).
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["proactive"] = "proactive"
    request_id: str
    event_id: str
    transcript: str


Trigger = Annotated[Union[UserRequest, Proactive], Field(discriminator="kind")]


class PlannerContext(BaseModel):
    """The single input the Planner needs from the HTTP layer.

    The HTTP route builds this from the request DTO; the Planner never
    reads the wire shape. This is the *narrow context* (M2) — the
    Planner doesn't know about the HTTP layer, the user, or anything
    outside the trigger + session + request_id.

    The trigger is a tagged union: :class:`UserRequest` (the user
    asked a question) vs :class:`Proactive` (the server noticed a
    session just finished extracting). The Planner's behavior is
    source-agnostic except for the proactive trigger's
    ISSUE_COMMAND prohibition (see :mod:`openrecall_server.agent.planner`).
    """

    model_config = ConfigDict(frozen=True)
    request_id: str
    trigger: Trigger
    session_id: str | None = None
    limit: int = 10


class PlannerResult(BaseModel):
    """The single source of truth for one Planner run (INV-9).

    The HTTP layer reads this object via the mapper (N1.2) and never
    imports any other domain type. The audit logger reads it for the
    audit entry. The agent LLM prompt is built before this object is
    constructed; this object is the post-prompt, post-decision view.
    """

    model_config = ConfigDict(frozen=True)
    request_id: str
    retrieval_trace_id: str
    audit_id: str | None = None
    outcome: PlannerOutcome
    answer: str | None = None
    confidence: float | None = None
    confidence_band: str | None = None
    atom_ids: tuple[str, ...] = Field(default_factory=tuple)
    refusal_reason: RejectionReason | None = None
    refusal_message: str | None = None
    # P2-commands: present iff outcome == ISSUE_COMMAND. Carries the
    # command_id of the newly-dispatched command. The mapper (N1.2)
    # translates this to the wire-level payload; the Android command
    # lifecycle UI watches the dispatcher for status changes.
    command_id: str | None = None
    command_status: str | None = None  # initial lifecycle status (PENDING)
    # End-to-end latency breakdown, observed by the Planner.
    retrieval_latency_ms: int = 0
    llm_latency_ms: int = 0
    validator_latency_ms: int = 0
    guardrails_latency_ms: int = 0
    total_latency_ms: int = 0
    # For the wire: the scored atoms referenced by the answer (used by
    # the mapper to build the chip list). The mapper truncates text to
    # :data:`~openrecall_server.http.routes.dto.MAX_ATOM_CHIP_TEXT_LEN`.
    atoms: tuple[ScoredAtom, ...] = Field(default_factory=tuple)
