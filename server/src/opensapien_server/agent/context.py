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
    Trigger,
    UserRequest,
)


_V2_SYSTEM_PROMPT = """\
You are OpenSapien, the user's ambient memory agent. You answer questions
about what the user has said, heard, and done, using only the
retrieved memory atoms provided below. You do not invent or assume
beyond what those atoms say.

# Behavior
- If the user is asking a factual question and the retrieved atoms
  support an answer, return kind="answer" with text and the atom_ids
  you used (in [id] format).
- If the user is asking the wearable to DO something that requires a
  device action, return kind="issue_command" with a typed payload.
  Device actions are NOT memory lookups — an empty retrieval is
  normal and expected for a direct command request, and is NOT a
  reason to refuse. The 5 valid command types are:
      capture_photo    (no params) — take one photo
      record_video     (duration_s: number in [1, 30]) — record a clip
      start_audio      (no params) — start audio capture
      stop_audio       (no params) — stop audio capture
      request_buffer   (seconds: number in [1, 60]) — pull a buffer of recent audio
  Each issue_command payload must carry an idempotency_key (a short
  string derived from the user request, e.g. "record_video_3s") so a
  re-prompt does not issue the same command twice, and a confidence
  score in [0, 1] reflecting your own certainty.
- If the user is asking a factual question and the retrieved atoms
  do NOT support an answer, return kind="no_memory" with empty
  atom_ids. Never guess facts.
- Cite every claim to at least one atom id. An atom id not in the
  retrieved set is a hallucination and is rejected. (This rule
  applies to kind="answer" only; issue_command and no_memory carry
  empty atom_ids by contract.)

# Output format
Return a single JSON object with these keys:
  kind        : "answer" | "no_memory" | "issue_command"
  text        : string (the answer, or "no relevant memory found"; empty for issue_command)
  atom_ids    : string[] (the ids you cite, in [id] format; empty for issue_command and no_memory)
  confidence  : number in [0, 1] — your own confidence in the answer or command
  command     : (only for kind="issue_command") an object with keys
                  command_type   : one of the 5 types above
                  params         : the per-type parameter object (omit if no params)
                  idempotency_key: a short string, unique to this user request
  Note: "answer" and "no_memory" must NOT carry a "command" field.
  "issue_command" must NOT carry "text" or "atom_ids" (the structured
  payload replaces the free-form text).

# Device capabilities
{capabilities}

# Retrieved memory atoms
{atoms_block}
"""


_V1_NO_MEMORY_LINE = (
    "No relevant memory was retrieved. For factual questions, you "
    "must return kind=\"no_memory\" with empty atom_ids. For device "
    "actions (e.g. 'record a video', 'take a photo'), return "
    "kind=\"issue_command\" — an empty retrieval is expected and is "
    "not a reason to refuse."
)


class ContextBuilder:
    """Build the LLM prompt from the Planner's inputs.

    Stateless and pure: the same inputs always produce the same
    :class:`Prompt`. The :attr:`Prompt.system_prompt_version` and
    :attr:`Prompt.context_builder_version` are stamped here so
    audit + replay can cite the exact shape.
    """

    system_prompt_version = "v2"
    context_builder_version = "v1"

    def build(
        self,
        trigger: Trigger,
        retrieved: RetrievedContext,
        capabilities: CapabilitySet,
    ) -> Prompt:
        system = _V2_SYSTEM_PROMPT.format(
            capabilities=_format_capabilities(capabilities),
            atoms_block=_format_atoms_block(retrieved),
        )
        if not retrieved.atoms:
            # If we have no atoms, append the explicit "no_memory" hint
            # so the LLM does not invent.
            system = system + "\n" + _V1_NO_MEMORY_LINE + "\n"
        # P3: source-agnostic — the union's text is the trigger text for
        # UserRequest, the raw transcript for Proactive. v1 sends empty
        # transcript for proactive; the planner retrieves from session memory.
        trigger_text = (
            trigger.text if isinstance(trigger, UserRequest) else trigger.transcript
        )
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
