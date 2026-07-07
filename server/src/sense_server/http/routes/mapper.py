"""Pure mapper: PlannerResult + RetrievedContext → wire DTOs (N1.2 / INV-8).

The mapper is a pure function over its inputs. It does no I/O, holds
no state, and imports nothing outside the DTO module and the
PlannerResult / RetrievedContext domain types. The HTTP layer is
the only caller.

The mapper is responsible for:

  * truncating chip text to :data:`MAX_ATOM_CHIP_TEXT_LEN` characters;
  * mapping :class:`PlannerOutcome` to the wire-level ``outcome`` string;
  * building the :class:`AgentResponseDTO` from the :class:`PlannerResult`;
  * carrying the request_id / retrieval_trace_id / audit_id through
    so the client can correlate.

It is *not* responsible for picking the chip atoms — that is the
Planners decision; the mapper just renders what the Planner decided.
"""
from __future__ import annotations

from .dto import (
    AgentResponseDTO,
    AtomChipDTO,
    MAX_ATOM_CHIP_TEXT_LEN,
)
from ...contracts.types import (
    PlannerOutcome,
    PlannerResult,
    ScoredAtom,
)


def _truncate(text: str, limit: int = MAX_ATOM_CHIP_TEXT_LEN) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _chip_for(atom: ScoredAtom) -> AtomChipDTO:
    return AtomChipDTO(
        atom_id=atom.atom_id,
        session_id=atom.session_id,
        kind=atom.kind,
        text=_truncate(atom.text),
        created_at=atom.created_at,
        start_ms=atom.start_ms,
        score=atom.score,
    )


def map_planner_result_to_dto(result: PlannerResult) -> AgentResponseDTO:
    """Translate one :class:`PlannerResult` into its wire DTO.

    Pure function. The :class:`PlannerResult` is the *only* input;
    chip selection and answer text come from the result, not from any
    other side channel.
    """
    atoms_by_id = {a.atom_id: a for a in result.atoms}
    chips = [
        _chip_for(atoms_by_id[aid])
        for aid in result.atom_ids
        if aid in atoms_by_id
    ]
    if result.outcome == PlannerOutcome.RETURN:
        outcome_wire = "return"
    elif result.outcome == PlannerOutcome.RETURN_WITH_UNCERTAINTY:
        outcome_wire = "return_with_uncertainty"
    else:
        outcome_wire = "refuse"
    return AgentResponseDTO(
        request_id=result.request_id,
        retrieval_trace_id=result.retrieval_trace_id,
        audit_id=result.audit_id or "",
        outcome=outcome_wire,  # type: ignore[arg-type]
        answer=result.answer,
        atoms=chips,
        confidence=result.confidence,
        confidence_band=result.confidence_band,  # type: ignore[arg-type]
        refusal_reason=result.refusal_message if result.outcome == PlannerOutcome.REFUSE else None,
    )
