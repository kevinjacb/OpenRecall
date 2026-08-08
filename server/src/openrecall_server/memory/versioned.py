"""Memory atom versioning — :func:`stamp_version_metadata` (M4.1).

The function is a binding no-op for atoms at the current pipeline
version. Future re-extraction paths (a v2 extractor, a new embedding
model) will produce *new* atoms that supersede the old ones via
:meth:`openrecall_server.memory.atom.MemoryAtom.to_provenance` and the
``Provenance.supersedes_atom_id`` field — atoms are never mutated in
place (INVARIANT 5).
"""
from __future__ import annotations

from .atom import MemoryAtom

# Canonical v1 metadata. When the pipeline advances, this constant moves
# and the corresponding supersession path becomes the new home for
# version-bumping logic.
_V1_EXTRACTION = "v1"
_V1_PROMPT = "v1"
_V1_SOURCE_PIPELINE = "transcript"


def stamp_version_metadata(atom: MemoryAtom) -> None:
    """Idempotently assert that ``atom`` carries the v1 version metadata.

    MemoryAtoms are frozen (pydantic ``frozen=True``), so this function
    cannot mutate the input. It exists to make the pipeline's no-op
    intent explicit at the call site — a future v2 entry point can
    replace the function body with one that constructs a new atom
    carrying the new version metadata and the
    ``supersedes_atom_id`` provenance link.
    """
    # No-op: the default atom already carries v1 metadata, and the
    # pydantic model is frozen. This call documents the contract.
    del atom
