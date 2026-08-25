# Speech→Command Channel — Design

**Status:** Proposed (awaiting implementation plan)
**Date:** 2026-08-25
**Author:** Kevin + Claude

## Problem

There is no path from spoken audio to device command execution. The only
command-issuing trigger is `UserRequest`, constructed solely at
`http/routes/agent.py:41` (`POST /agent`). The Android app has no `/agent`
endpoint, and no server code builds a `UserRequest` from a transcript. The
proactive path — the only thing that fires on transcripts — is forbidden
from `ISSUE_COMMAND` (`planner.py:231`, `PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND`).
So a spoken "take a photo of mine" cannot execute today. This is an
architectural gap, not a regression from the sentence_id/coalescer work.

The user requires:
1. Spoken commands execute **immediately** (the user reports "take a photo"
   not executing at all / slowly).
2. Detection is **not rigid** — a scoped LLM confirms intent, so phrasing
   variations work and narration ("I told him to take a photo") does not fire.
3. A **memory is produced for every executed command**.
4. Day-one scope: photo / video / audio commands. Reminders and other
   configurable actions are a later expansion.

## Non-goals

- On-device (wake-word) command detection. Server-side detection from the
  transcript is the pragmatic path given the ESP-IDF + BLE-transport
  architecture; on-device is a separate, larger effort.
- Reminders / arbitrary configurable actions in this iteration. These are a
  localized future change (see Extensibility), not a free consequence of this
  design.
- Changing the proactive-hallucination guard. The `Proactive` trigger stays
  forbidden from `ISSUE_COMMAND`. This channel is a *separate* path.

## Architecture

A new `CommandDetector` listens on the per-transcript event stream (not the
60s extraction window) and runs a two-stage pipeline: a free keyword-phrase
pre-filter, then a scoped LLM that outputs a structured command directly.
Confirmed commands dispatch through the **existing rule-based command path**
(validator + guardrails + dispatcher) — **not** through the conversational
Planner. The Planner is the wrong shape here: it does retrieval + a general
LLM action-decision, which is pointless for a one-shot confirmed command and
doubles the LLM cost. A command memory is written when the device
**acknowledges** the command, via the existing ack hook.

```
 audio → AudioIngestPipeline → coalesced Segment
   → GatewayCore._emit (per stored transcript event)
       ├─→ ExtractionEnqueuer        (existing; 60s-window memory path)
       └─→ CommandDetector.feed(event)   (NEW; immediate)
              │
              ├─ Stage 1: keyword-phrase match over a rolling recent-text buffer
              │           (sync, free; misses → stop, no LLM cost)
              │
              └─ Stage 2: scoped LLM  →  {command:{type,params}|null, confidence}
                       (async, asyncio.to_thread wrapping sync ChatModel.complete;
                        constrained to CommandType literals; threshold-gated)
                              │
                              └─ on confirm: build Command
                                  → StrictCommandValidator (rule-based)
                                  → StrictCommandGuardrails (capability + battery)
                                  → CommandDispatcher.issue (signs + tracks)
                                        │
   device ← CommandMessage relayed over WS (existing path, unchanged)

   device → CommandAck
   → GatewayCore._on_command_ack  (existing hook, core.py:408)
       └─ CommandMemoryWriter (NEW): append a `command_event` MemoryAtom
                                  (atom_id = command_id → natural idempotency)
```

## Components

### 1. CommandDetector (`src/openrecall_server/agent/command_detector.py`, new)

**Responsibility:** per-transcript candidate detection + LLM confirmation +
dispatch. Fire-and-forget off the audio path.

**Attach point:** `GatewayCore._emit` (`gateway/core.py:596`), as a sibling
to `self._enqueuer.enqueue(...)`. On each *stored* transcript event, if a
detector is configured, call `detector.feed(session_id, event)`. This is the
per-sentence seam — it fires within ~240ms (hop) + sentence-close of speech,
well under the 60s extraction window.

**Constructor dependencies (all already constructed in `run_gateway.py`):**
- `model: ChatModel` — the OpenAI-compatible client (same
  `OPENRECALL_LLM_*` config the extractor uses; can be a separate, smaller
  model via its own env vars — see Config).
- `dispatcher: CommandDispatcher`
- `command_validator: StrictCommandValidator`
- `command_guardrails: StrictCommandGuardrails`
- `loop: asyncio.AbstractEventLoop` — for scheduling Stage 2 off the audio path.
- `capability_provider: CapabilityProvider` — required by the guardrails; same
  instance the Planner uses.
- `speaker_registry: SpeakerRegistry` — to resolve the wearer flag for the
  confirmed-wearer gate (same registry `GatewayCore._emit` already uses at
  `core.py:612`).

**Confirmed-wearer gate (resolved decision #2):** `feed` runs Stage 1 only if
the triggering event is from the **confirmed wearer**:
`event.speaker is not None and event.speaker_assignment == "confirmed" and
speaker_registry.get(event.speaker).is_wearer is True`. A non-wearer (someone
else near the device) saying "take a photo" does **not** fire. If speaker
recognition is off (`event.speaker is None`) or no wearer is confirmed, voice
commands are disabled — this is the "mature, accurate" requirement: commands
fire only for the identified wearer, never for ambient others. The gate is
configurable via `OPENRECALL_COMMAND_REQUIRE_WEARER` (default **true**); set
false only for isolated testing. **This makes the channel depend on the
speaker-recognition system's accuracy** — see Dependencies & risks.

**`feed(session_id, event) -> None`:**
1. Append `event.text` to a per-session rolling text buffer (capped, e.g. last
   ~10 segments) and a per-session recent-segment window (for Stage 2 context).
2. **Stage 1 (sync, free):** match command *phrases* (not bare words) against
   the rolling buffer. The vocabulary is phrases: `take a photo`, `take a
   picture`, `snap a pic`, `capture a photo/picture`, `record a video`,
   `start a video`, `stop the video / recording`, `start audio / recording`,
   `stop audio / recording`, `flush snapshots`. Matching the rolling buffer
   (not the single segment) handles imprecise coalescing that splits a phrase
   across two sentences ("take a" … "photo"). **Bare common words (`stop`,
   `start`, `video`, `audio`) are NOT standalone triggers** — they appear
   constantly in speech and would defeat the filter + load Ollama.
3. On miss → return (no LLM cost). On hit → **dedup guard**: if a candidate
   for `(session_id, matched-command-type-bucket, normalized buffer tail)` is
   already in-flight or within the cooldown window (env-tunable, default 3s),
   return. This stops a single spoken command (which may surface across
   overlapping retranscribed segments) from firing Stage 2 twice.
4. On a fresh candidate → schedule Stage 2 via
   `loop.call_soon_threadsafe(self._schedule_stage2, session_id, context)`.

**`_schedule_stage2` (async, runs on the gateway loop):**
- If the in-flight Stage 2 cap is reached (default 1; env-tunable), drop this
  candidate and log — this is the contention guard against re-living the
  `proactive_empty_batch_contention` / `proactive_plan_timeout` incidents.
  Stage 2 must never starve the shared Ollama model that extraction + the
  proactive planner use.
- Call `await asyncio.to_thread(self._model.complete, system_prompt, user)`
  (wrapping the sync `ChatModel.complete`).

**Stage 2 prompt (scoped, structured):**
- System: "You classify whether a wearer is directly commanding their own
  wearable device. Narration, quotes, questions, hypotheticals, and
  third-person mentions are NOT commands. If it is a direct command, output
  JSON `{"command": {"type": <one of CommandType literals>, "params": {}},
  "confidence": <0..1>}`. If not, output `{"command": null, "confidence":
  <0..1>}`. Only these types are valid: capture_photo, start_video, stop_video,
  start_audio, stop_audio, record_video, flush_snapshots. params is always
  `{}` for these types."
- User: the rolling buffer of recent transcript text (the candidate sentence
  + a few sentences of context), so the model can tell narration from a
  direct command.
- Parse strictly (`json.loads` + schema check); on any parse failure or
  `command: null` → return. On `confidence < threshold` (env-tunable, default
  0.8) → return.

**On confirm:** reuse the **same rule-based dispatch sequence** the Planner
runs in `_dispatch_command` (`planner.py:308`), operating on the Stage 2
structured output instead of the Planner's parsed `IssueCommandPayload`:
1. Build the validator input from the Stage 2 `{type, params}` (the exact
   object shape `StrictCommandValidator` expects — pinned in the plan; today
   it validates an `IssueCommandPayload`).
2. `StrictCommandGuardrails.check(command, capability_snapshot)` — capability
   (`capture_photo` needs `camera`, etc.) + battery floor (5% for quick ops).
   This takes the constructed `Command` (`command.command_type`).
3. Build the `Command` (fresh `command_id`, `session_id`, `issued_at` /
   `expires_at`, `idempotency_key`) and `CommandDispatcher.issue(command)` —
   signs + tracks; returns `SignedCommand`.

No Planner, no retrieval. The guardrails read the live capability/resource
snapshot from the same `capability_provider` the Planner uses, so a voice
command is gated identically to a `/agent` command (won't fire if no camera /
battery dead).

The issued `command_id` and the `source_event_id` (the transcript event that
triggered) are recorded in a per-session `pending_command_provenance` map so
the ack hook can link the eventual memory back to the spoken transcript.

### 2. CommandMemoryWriter (`src/openrecall_server/agent/command_memory.py`, new)

**Responsibility:** write one `command_event` memory atom when the device
acks a command.

**Attach point:** `GatewayCore._on_command_ack` (`gateway/core.py:408`), the
existing hook that fires when a `CommandAck` arrives. After
`self._dispatcher.ack(command_id)`, if a memory writer is configured and the
`command_id` is in `pending_command_provenance`, call
`memory_writer.on_ack(command_id, session_id, command_type, source_event_id)`.

**Why ack, not dispatch:** issuing ≠ executing. Writing the memory at dispatch
would record "took a photo" for a command that never reached the device. The
ack is the device confirming receipt; it is the earliest reliable execution
signal. A command that is never acked produces no memory — which also
surfaces the "not executing at all" symptom diagnostically (issued command,
no ack, no memory).

**Atom shape** (`MemoryAtom`, `memory/atom.py`):
- `atom_id = command_id` — the command id is globally unique, so this is a
  natural idempotency key: one memory per command, even under retry/reissue.
- `kind = "command_event"` — a distinctly typed atom, separable from ambient
  transcript-window memories. This is the conscious decision that keeps voice
  commands from reading as "random" duplicate memories (Flaw #4).
- `source_event_id` = the transcript event that triggered detection.
- `source_pipeline_version = "command"` — distinguishes from
  `transcript` / `vision` atoms.
- `text` = a human record, e.g. `Took a photo (you said: "take a photo of
  mine")` — assembled from the command type + the triggering transcript text.
- `session_id`, `created_at`, `start_ms` from the source event.
- `extraction_version` left at default; the atom is not subject to the
  per-session extraction cursor (it bypasses the 60s windowing by design).

**Interaction with the ambient 60s extractor:** the extractor will also see
the "take a photo" transcript in a closed window and may produce its own
memory of the surrounding conversation. This is intended and not a duplicate:
the ambient memory records the *conversation*, the `command_event` records
the *device action with execution provenance*. They carry different `kind`
and `source_pipeline_version`, so the UI/retrieval can distinguish them.

### 3. GatewayCore wiring (`src/openrecall_server/gateway/core.py`, modify)

- Constructor: add `command_detector: CommandDetector | None = None` and
  `command_memory_writer: CommandMemoryWriter | None = None` (None-defaulted
  for back-compat with tests that don't wire them, mirroring the existing
  `enqueuer`/`dispatcher` pattern).
- `_emit`: after the existing `if stored and self._enqueuer is not None:`
  block, add `if stored and self._command_detector is not None and
  self._session_id is not None: self._command_detector.feed(self._session_id, event)`.
- `_on_command_ack`: after `self._dispatcher.ack(...)`, add the memory-writer
  call described above.
- The detector needs the gateway loop. `GatewayCore` runs on an asyncio loop;
  the loop is captured at first `feed` via `asyncio.get_running_loop()` on the
  gateway thread (or passed in at construction from `run_gateway`, which has
  it). The detector stores `loop.call_soon_threadsafe` to schedule Stage 2
  off the synchronous `_emit` path.

### 4. run_gateway wiring (`scripts/run_gateway.py`, modify)

- Construct the `CommandDetector` with the shared `ChatModel` (or a dedicated
  smaller model — see Config), the shared `dispatcher`, `command_validator`,
  `command_guardrails`, `capability_provider`, and the gateway loop.
- Construct the `CommandMemoryWriter` with the shared `atom_store`.
- Pass both into `GatewayCore(...)`.

### 5. Config (`settings`, new env vars, `OPENRECALL_*`)

- `OPENRECALL_COMMAND_DETECTOR_ENABLED` (bool, default false) — off by
  default, mirroring the speaker-recognition off-by-default pattern.
- `OPENRECALL_COMMAND_REQUIRE_WEARER` (bool, default **true**) — gate voice
  commands on the confirmed wearer. Default true is the mature/accurate
  behavior; false is for isolated testing only.
- `OPENRECALL_COMMAND_PHRASES` (comma/newline-separated string, optional) —
  the Stage 1 phrase vocabulary as a **data-driven config list, not code
  constants** (resolved decision #3), so phrases grow without code changes.
  A built-in default list ships in code; this env var overrides/appends.
- `OPENRECALL_COMMAND_LLM_MODEL` / `OPENRECALL_COMMAND_LLM_BASE_URL` /
  `OPENRECALL_COMMAND_LLM_API_KEY` — optional; if unset, the detector
  reuses the extractor's `OPENRECALL_LLM_*` model. Lets operators point Stage
  2 at a smaller/faster model (e.g. qwen2.5:3b) while extraction keeps its
  own.
- `OPENRECALL_COMMAND_CONFIDENCE_THRESHOLD` (float, default 0.8).
- `OPENRECALL_COMMAND_COOLDOWN_S` (float, default 3.0) — per-session dedup
  window.
- `OPENRECALL_COMMAND_MAX_INFLIGHT` (int, default 1) — the contention cap.

## Extensibility (reminders later) — stated honestly

Adding reminders / other configurable actions later is **not free**. It is a
localized change to this channel, not a consequence of the architecture:
- Widen the Stage 2 output schema to emit non-command actions
  (`{"action": {"kind": "create_reminder", ...}}`).
- Add a dispatch branch in the detector for each new action kind (e.g. call
  the existing reminder-creation path).
- Widen Stage 1 phrases ("remind me to …").

This re-opens a portion of the hallucination surface (the LLM now emits more
than device commands), which is managed by the same confidence threshold +
scoped prompt + cooldown, not by the proactive guard (which still only
governs the proactive path).

## Tradeoffs (acknowledged)

- **Two LLM calls per confirmed command? No — one.** Stage 1 is free; Stage 2
  is the single LLM call; dispatch is rule-based. This is the fix to Flaw #1
  (the original design routed through the Planner, adding a second LLM call +
  retrieval).
- **Latency:** coalesce (~0.3–1.5s, dominated by sentence-close) + Stage 2 LLM
  (~0.3–0.8s local) + rule-based dispatch (~0.05s) ≈ **0.7–2.3s** end-to-end.
  The coalescer, not the detector, dominates. The detector's own contribution
  is roughly halved vs. the Planner-routing design.
- **Stage 2 is a cost/filter AND a correctness gate.** Unlike the original
  design (where I admitted it wasn't a correctness crutch), here Stage 2 is
  load-bearing: it is the only LLM judgment, and it outputs the structured
  command. The Planner is not in the path to backstop it.
- **Ollama contention:** defended by the cooldown + in-flight cap (default 1).
  Stage 2 never runs on normal speech (Stage 1 misses), and never more than
  one at a time per process.
- **Confidence threshold is a guess:** env-tunable, default 0.8, conservative
  but not blocking. Tunable live without restart if it over/under-fires.

## Testing strategy

- **CommandDetector, unit:** Stage 1 phrase matching (hit/miss, split-phrase
  across buffer, bare-word non-trigger); dedup/cooldown; in-flight cap; Stage 2
  mock-LLM returns command vs null vs low-confidence vs malformed; dispatch
  builds the right `Command`; guardrail refusal (no camera) does not dispatch;
  **confirmed-wearer gate** — a confirmed-wearer event proceeds, a
  non-wearer or `speaker_assignment != "confirmed"` or `speaker is None`
  event is dropped before Stage 1; `OPENRECALL_COMMAND_REQUIRE_WEARER=false`
  relaxes the gate. Uses a fake `ChatModel` (scripted `complete` returns),
  a fake dispatcher, and a fake `SpeakerRegistry`.
- **CommandMemoryWriter, unit:** writes a `command_event` atom on ack; atom_id
  = command_id (re-ack is a no-op idempotent write); no write when the
  command isn't in the provenance map (e.g. a `/agent`-issued command, which
  has no transcript provenance — those memories are out of scope here).
- **GatewayCore, integration:** a stored transcript event feeds the detector;
  a `CommandAck` writes the memory; the existing extraction path is
  unaffected (regression guard: the 60s-window memory path still runs).
- **Off-by-default:** with `OPENRECALL_COMMAND_DETECTOR_ENABLED=false`, no
  detector is wired; behavior is identical to today.

## Resolved decisions

1. **Dedup key** (approved): reuse `UuidIdGenerator` (same as `/agent`) for
   `command_id`; derive `idempotency_key` from
   `(session_id, command_type, normalized-trigger-text, cooldown-bucket)` so
   a repeated identical command within the cooldown dedups at the dispatcher
   too.
2. **Confirmed-wearer gate** (approved, strengthened): voice commands fire
   **only** for the confirmed wearer. A non-wearer near the device must not
   trigger it. This requires speaker recognition to be enabled and a wearer
   to be confirmed/enrolled. The gate is configurable
   (`OPENRECALL_COMMAND_REQUIRE_WEARER`, default true) but the default is the
   mature/accurate behavior.
3. **Stage 1 phrases** (approved): data-driven config list
   (`OPENRECALL_COMMAND_PHRASES`), env-overridable, with a built-in default —
   not code constants.

## Dependencies & risks

- **Depends on speaker-recognition accuracy.** The confirmed-wearer gate
  inherits whatever accuracy the speaker-ID system has on real hardware. Per
  project memory, the real-voice device validation of `SpeakerEmbedder` is
  still pending, and there is a current regression: **Android is not
  labelling identified speakers** (see Follow-up). Until that is resolved,
  voice commands will be unreliable on real hardware (the gate will misfire
  or block). This is an honest prerequisite, not something this channel can
  fix internally — the channel is correctly gated; the speaker system must
  be made accurate as a separate effort.
- **Off-by-default + wearer-gated means no voice commands out of the box
  without speaker setup.** This is intentional (mature/accurate). An operator
  must enable `SENSE_SPEAKER_ENABLED`, enroll the wearer ("You"), and enable
  `OPENRECALL_COMMAND_DETECTOR_ENABLED` before voice commands work.
- **Ollama contention** — defended by the cooldown + in-flight cap; called
  out in Tradeoffs.

## Follow-up (after this work lands)

**Analyze the Android speaker-labelling regression.** The user reports
identified-speaker labels are missing again in the Android app. Per memory
`android-speaker-recognition-ui`, the wiring (SpeakerCache seeded from
`GET /speakers`, ChatViewModel.nameSpeaker reading the live session id from
RelayController) was landed in commit c4499a5. The regression is likely a
wiring gap introduced since. This is a separate investigation to run after
the command channel lands — it is explicitly **out of scope for this plan**,
but it is the immediate next task because the command channel's
confirmed-wearer gate depends on speaker recognition being correct
end-to-end (server identification → Android labelling → wearer
confirmation).