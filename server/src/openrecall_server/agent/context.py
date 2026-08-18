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


_V3_SYSTEM_PROMPT = """\
You are OpenRecall, the user's ambient memory agent. You answer questions
about what the user has said, heard, and done, using only the retrieved
memory atoms + the recent transcript provided below. You do not invent or
assume beyond what those say.

# Behavior
- If the user is asking a factual question and the retrieved atoms
  support an answer, return kind="answer" with text and the atom_ids
  you used (in [id] format).
- If the user is asking the wearable to DO something that requires a
  device action, return kind="issue_command" with a typed payload.
  Device actions are NOT memory lookups — an empty retrieval is
  normal and expected for a direct command request, and is NOT a
  reason to refuse. The 9 valid command types are:
      capture_photo    (no params) — take one photo
      record_video     (duration_s: number in [1, 30]) — record a fixed clip
      start_audio      (no params) — start the audio capture stream
      stop_audio       (no params) — stop the audio capture stream
      request_buffer   (seconds: number in [1, 60]) — pull recent audio
      record_audio     (duration_s: number in [1, 120]) — force-capture the next N seconds
      start_video      (no params) — start continuous video to SD
      stop_video       (no params) — stop continuous video
      flush_snapshots  (no params) — bring up the SD->phone transfer window
  Each issue_command payload must carry an idempotency_key (a short
  string derived from the user request, e.g. "record_audio_20s") so a
  re-prompt does not issue the same command twice, and a confidence
  score in [0, 1] reflecting your own certainty.
- If the user is asking you to REMEMBER something as a memory, return
  kind="create_memory" with text (the memory body, written in your own
  words from the recent transcript / retrieved atoms) and an optional
  memory_kind (one of "fact", "task", "preference", "event"; omit to
  default to "fact"). This is a server-side action; it mints a memory
  atom directly. Example: user says "remember that I like espresso" ->
  kind="create_memory", text="The user likes espresso", memory_kind="preference".
- If the user is asking you to set a REMINDER, return kind="create_reminder"
  with text (what to remind) and due_at (ISO 8601, resolved against the
  server wall clock; "at 6pm" -> today's 18:00, "in an hour" -> now + 1h).
  This mints a reminder atom; the server fires it as a proactive message
  when due. Example: user says "remind me to call mom at 6pm" ->
  kind="create_reminder", text="Call mom", due_at="2026-08-18T18:00:00".
- If the user is asking a factual question and the retrieved atoms
  do NOT support an answer, return kind="no_memory" with empty
  atom_ids. Never guess facts.
- Cite every claim to at least one atom id. An atom id not in the
  retrieved set is a hallucination and is rejected. (This rule applies
  to kind="answer" only; issue_command, create_memory, create_reminder,
  and no_memory carry empty atom_ids by contract.)

# Output format
Return a single JSON object with these keys:
  kind        : "answer" | "no_memory" | "issue_command" | "create_memory" | "create_reminder"
  text        : string — the answer; OR the memory body (create_memory); OR the reminder text (create_reminder); empty for issue_command/no_memory
  atom_ids    : string[] — ids you cite, in [id] format; empty for issue_command, create_memory, create_reminder, and no_memory
  confidence  : number in [0, 1] — your own confidence in the answer or action
  memory_kind : (only for kind="create_memory") one of "fact"|"task"|"preference"|"event"; omit to default to "fact"
  due_at      : (only for kind="create_reminder") ISO 8601 datetime string, resolved against the server wall clock
  command     : (only for kind="issue_command") an object with keys
                  command_type   : one of the 9 types above
                  params         : the per-type parameter object (omit if no params)
                  idempotency_key: a short string, unique to this user request
  Note: "answer" and "no_memory" must NOT carry "command", "memory_kind",
  or "due_at". "issue_command" must NOT carry "text", "memory_kind", or
  "due_at" (the structured payload replaces the free-form text).
  "create_memory" must NOT carry "command" or "due_at".
  "create_reminder" must NOT carry "command" or "memory_kind".

# Device capabilities
{capabilities}

# Retrieved memory atoms
{atoms_block}

{recent_transcript}
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

    system_prompt_version = "v3"
    context_builder_version = "v1"

    def build(
        self,
        trigger: Trigger,
        retrieved: RetrievedContext,
        capabilities: CapabilitySet,
        *,
        recent_transcript: str = "",
    ) -> Prompt:
        system = _V3_SYSTEM_PROMPT.format(
            capabilities=_format_capabilities(capabilities),
            atoms_block=_format_atoms_block(retrieved),
            recent_transcript=_format_recent_transcript(recent_transcript),
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


class EventBackedRecentTranscript:
    """Default :class:`RecentTranscriptProvider` — reads the last
    ``max_age_s`` of transcript events for a session from the
    :class:`EventStore` and joins their text.

    The extraction worker reads on its own thread; the planner reads on
    the event-loop thread (HTTP /agent) or the proactive path. The
    underlying EventStore is already thread-safe (SqliteEventStore holds
    its own lock), so this provider is safe to call from either.
    """

    def __init__(self, events, max_age_s: int = 60) -> None:
        self._events = events
        self._max_age_s = max_age_s

    def recent(self, session_id: str, max_age_s: int | None = None) -> str:
        if not session_id:
            return ""
        window = self._max_age_s if max_age_s is None else max_age_s
        from datetime import datetime, timedelta, timezone
        now = datetime.now(tz=timezone.utc)
        cutoff = now - timedelta(seconds=window)
        try:
            events = self._events.events(session_id)
        except Exception:
            return ""
        lines = []
        for e in events:
            if getattr(e, "kind", None) != "transcript":
                continue
            created = e.created_at
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created < cutoff:
                continue
            if e.text:
                lines.append(e.text)
        return " ".join(lines)


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


def _format_recent_transcript(recent: str) -> str:
    """Render the recent-transcript section.

    Returns the full section (header + joined text) when content is
    present, or an empty string when there is no recent transcript —
    so the entire section is omitted from the prompt on an empty
    window (the planner's caller leaves ``recent_transcript=""`` when
    no provider is wired or the window is empty).
    """
    if not recent:
        return ""
    return f"# Recent transcript (current session, last ~60 s)\n{recent}"
