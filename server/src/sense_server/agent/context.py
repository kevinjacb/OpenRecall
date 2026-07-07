"""ContextBuilder — turns retrieved atoms + capabilities into an LLM prompt (H1).

The v1 system prompt is short, explicit, and the binding of the
"Server owns intelligence, wearable owns sensing" principle. The
planner references the :class:`Prompt`'s
``system_prompt_version`` and ``context_builder_version`` so audit +
replay can cite the exact prompt shape that produced any given
action.
"""
from __future__ import annotations

from ..contracts.types import (
    CapabilitySet,
    Prompt,
    RetrievedContext,
)


_V1_SYSTEM_PROMPT = """\
You are Sense, the user's ambient memory agent. You answer questions
about what the user has said, heard, and done, using only the
retrieved memory atoms provided below. You do not invent or assume
beyond what those atoms say.

# Behavior
- If the retrieved atoms support an answer, return kind="answer" with
  text and the atom_ids you used (in [id] format).
- If the retrieved atoms do NOT support an answer, return
  kind="no_memory" with empty atom_ids. Never guess.
- Cite every claim to at least one atom id. An atom id not in the
  retrieved set is a hallucination and is rejected.

# Output format
Return a single JSON object with these keys:
  kind        : "answer" | "no_memory"
  text        : string (the answer, or "no relevant memory found")
  atom_ids    : string[] (the ids you cite, in [id] format)
  confidence  : number in [0, 1] — your own confidence in the answer

# Device capabilities
{capabilities}

# Retrieved memory atoms
{atoms_block}
"""


_V1_NO_MEMORY_LINE = (
    "No relevant memory was retrieved. You must return kind=\"no_memory\"."
)


class ContextBuilder:
    """Build the LLM prompt from the Planner's inputs.

    Stateless and pure: the same inputs always produce the same
    :class:`Prompt`. The :attr:`Prompt.system_prompt_version` and
    :attr:`Prompt.context_builder_version` are stamped here so
    audit + replay can cite the exact shape.
    """

    system_prompt_version = "v1"
    context_builder_version = "v1"

    def build(
        self,
        trigger_text: str,
        retrieved: RetrievedContext,
        capabilities: CapabilitySet,
    ) -> Prompt:
        system = _V1_SYSTEM_PROMPT.format(
            capabilities=_format_capabilities(capabilities),
            atoms_block=_format_atoms_block(retrieved),
        )
        if not retrieved.atoms:
            # If we have no atoms, append the explicit "no_memory" hint
            # so the LLM does not invent.
            system = system + "\n" + _V1_NO_MEMORY_LINE + "\n"
        user = _format_user(trigger_text, retrieved)
        return Prompt(
            system=system,
            user=user,
            system_prompt_version=self.system_prompt_version,
            context_builder_version=self.context_builder_version,
        )


def _format_capabilities(caps: CapabilitySet) -> str:
    return (
        f"camera={caps.camera}, microphone={caps.microphone}, "
        f"retrospective_buffer={caps.retrospective_buffer}, "
        f"display={caps.display}, speaker={caps.speaker}"
    )


def _format_atoms_block(retrieved: RetrievedContext) -> str:
    if not retrieved.atoms:
        return "(none)"
    lines = []
    for a in retrieved.atoms:
        lines.append(f"[{a.atom_id}] session={a.session_id} kind={a.kind} score={a.score:.3f} :: {a.text}")
    return "\n".join(lines)


def _format_user(trigger_text: str, retrieved: RetrievedContext) -> str:
    """The user-prompt half. Includes the trigger + a flat list of atom ids."""
    ids = [a.atom_id for a in retrieved.atoms]
    return (
        f"trigger: {trigger_text}\n"
        f"available_atom_ids: {ids}\n"
        f"return JSON only"
    )
