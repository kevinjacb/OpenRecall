# Android Speaker-Recognition UI — Design

> Status: design. Predecessor: the server-side speaker-recognition v1
> ([[active-plan-speaker-recognition-2026-07-26]]) and the real Resemblyzer
> backend ([[speaker-embedder-backend]]) both ship on `main`. This spec
> builds the **Android client** that renders and acts on speaker data —
> the piece that was never built (the "can't see the new additions on
> mobile" gap).

## Goal

Make speaker recognition visible and actionable on the Android relay app:
name unknown speakers, show speaker labels on transcripts, rename, correct
mislabels (reassign), and confirm the "You" wearer tag — all driven by the
server-side recognizer that already exists.

## Why now

The server emits speaker data but the Android app drops it:
- `transcript` §E carries `speaker`/`speaker_confidence`/`speaker_assignment`
  → Android only **logs** it (`RelaySession.kt:65-66`), never shows it.
- `proactive` with `propose={kind:"name_speaker",speaker_id}` → the nudge
  **text** renders in `ProactiveMessageBubble`, but `propose` is dropped
  (`ChatMessage` has no `propose` field) — no name-the-speaker UI, no
  outbound `name_speaker`/`reassign_speaker` control message.
- HTTP `/sessions/{id}/events` (`_event_to_wire`, `sessions.py:80`) doesn't
  include `speaker` at all, and the Android `CaptureEventDto` has no speaker
  field — so the Recordings screen can't show labels either.
- There is **no** `GET /speakers` endpoint, so the app can't list known
  speakers or resolve a UUID to a name.

## Architecture

**Server resolves speaker identity at read time and exposes a speakers
list; the app displays it and sends back lightweight control messages.**

- **Read-time name resolution.** The server adds `speaker_name` + `is_wearer`
  to the transcript §E message and to HTTP `_event_to_wire`, resolving the
  UUID via `SpeakerRegistry.get(speaker_id)` on the fly. Resolving at read
  time (not storing the name on the event row) means renames and reassigns
  reflect immediately in history without a backfill.
- **`GET /speakers` endpoint.** Returns the full registry (minus biometrics)
  so the app can seed/refresh a `SpeakerCache`, drive the reassign picker
  with all known speakers (not just those heard this run), and survive
  restart with names intact.
- **App `SpeakerCache`** (`{speaker_id → {name, is_wearer}}`): seeded from
  `GET /speakers`, updated by every transcript §E. Backs the Recordings
  labels and the reassign picker.
- **Naming and rename share one message.** `NameSpeaker{session_id,
  speaker_id, name}` — the server's `set_display_name` overwrites, so it
  works for both naming a "?" and renaming "Sarah"→"Sara".
- **Reassign** targets an existing `to_speaker_id` from the cache; v1 uses
  `scope="all"` (the server is session-scoped, so "all of this speaker" *is*
  "this conversation").
- **Off by default** unchanged: `SENSE_SPEAKER_ENABLED=false` → no speaker
  fields are populated, the app shows no speaker UI, zero behavior change.

## Server changes (additive, no migration, no new outbound type)

### 1. `protocol/messages.py` — `TranscriptMsg`
Add two optional fields (existing clients ignore unknown keys; this is
strictly additive on the wire):
```python
class TranscriptMsg(_Strict):
    type: Literal["transcript"] = "transcript"
    session_id: str
    text: str
    duration_ms: int
    speaker: str | None = None
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
    speaker_name: str | None = None      # NEW: resolved display_name
    is_wearer: bool = False              # NEW: this hop's speaker is the wearer
```
The gateway resolves these from `SpeakerRegistry.get(speaker)` when building
the outbound transcript (None/False when speaker is None or the registry has
no row). `SpeakerEmbedder`/`SpeakerIdentifier` already produce the UUID; the
gateway core already holds the registry. No pipeline change.

### 2. `http/routes/sessions.py` — `_event_to_wire`
Add `speakerName` + `isWearer`, resolved via the registry at read time. The
route gains access to the `SpeakerRegistry` (thread it through `build_app`
→ the route reads `request.app["speaker_registry"]`; additive wiring, same
pattern as the existing `event_store`/`atom_store` app keys). The stored
event row keeps only the UUID (so renames stay fresh — no backfill on
rename/reassign).
```python
def _event_to_wire(e, registry) -> dict:
    sp = registry.get(e.speaker) if e.speaker else None
    return {
        ...existing fields...,
        "speaker": e.speaker,
        "speakerName": sp.display_name if sp else None,
        "isWearer": sp.is_wearer if sp else False,
        "speakerConfidence": e.speaker_confidence,
        "speakerAssignment": e.speaker_assignment,
    }
```

### 3. `http/routes/speakers.py` — NEW `GET /speakers`
Returns the full registry **without biometrics** (no `centroid`,
`embedding_model`, `dim`). Shape:
```json
{ "speakers": [
  { "speakerId": "uuid", "displayName": "Sarah", "isWearer": false,
    "enrollmentStatus": "confirmed", "turnCount": 14,
    "firstSeen": "2026-07-29T...", "updatedAt": "2026-07-29T..." },
  ... ] }
```
Registered in `build_app` alongside the other routes. Reads
`request.app["speaker_registry"]`. Optional `?session_id=` filter is **not**
in v1 (the registry is global; the picker wants all known speakers).

**Biometric guarantee:** `centroid`/`embedding_model`/`dim` are never
serialized over HTTP. Speaker embeddings never leave the Mac unless
`SENSE_SPEAKER_EMBED_BASE_URL` is explicitly set (unchanged from
[[speaker-embedder-backend]]).

## Android changes

### 1. `protocol/Messages.kt`
- `Transcript`: add `speakerName: String?`, `isWearer: Boolean` (the
  `ignoreUnknownKeys` parser already drops unknown fields gracefully; this
  just maps the new ones).
- `Proactive`: parse `propose` into a sealed type:
  ```kotlin
  @Serializable data class NameSpeakerPropose(val speakerId: String)
  // propose = {"kind":"name_speaker","speaker_id":"..."}
  ```
  Unknown `propose.kind` → null (forward-compat).
- Outbound control messages + a `sendControl(msg)` over the WS (reuses the
  existing hello/bye send path in `RelaySession`):
  ```kotlin
  @Serializable data class NameSpeakerMsg(val sessionId: String, val speakerId: String, val name: String)
  @Serializable data class ReassignSpeakerMsg(val sessionId: String, val fromSpeakerId: String, val toSpeakerId: String, val scope: String)
  ```
  Both carry `type` discriminator for the server's `parse_control`.

### 2. `RelaySession.kt`
- `Transcript`: route to `SpeakerCache` (upsert `{speaker → name/is_wearer}`)
  in addition to the existing log line.
- `Proactive` with non-null `propose`: build a `ChatMessage` of kind
  `SPEAKER_NUDGE` carrying the `propose` + `sessionId` (not just the text).
- Add `sendControl(msg)`: serialize + send a JSON text frame over the WS.

### 3. `data/ChatHistoryStore.kt` + `ui/chat/ChatMessageList.kt`
- Add `ChatMessageKind.SPEAKER_NUDGE`.
- Add `propose: NameSpeakerPropose?` and `sessionId: String?` to `ChatMessage`.
- `ChatMessageList`'s exhaustive `when (msg.kind)` gets a `SPEAKER_NUDGE ->
  NameSpeakerBubble(...)` branch (compile error until added — the codebase
  enforces completeness).

### 4. `ui/chat/NameSpeakerBubble.kt` — NEW
The nudge becomes interactive: the proactive text + an inline text field +
a "Name" button. On submit → `relaySession.sendControl(NameSpeakerMsg(...))`
→ optimistic local update (SpeakerCache upsert + bubble collapses to a
plain "Named: Sarah" proactive line). No server ack exists; correctness
comes from the next transcript carrying the new `speaker_name`.

### 5. `ui/recordings/SessionDetailScreen.kt` (+ ViewModel)
- Render `speakerName` (or "?") on each transcript line.
- Tap a label → menu: **Rename** (dialog → `NameSpeakerMsg`) | **Actually
  someone else…** (picker from `SpeakerCache`/`GET /speakers` →
  `ReassignSpeakerMsg{from, to, scope="all"}`).

### 6. `data/SpeakerCache.kt` — NEW
In-memory `{speaker_id → SpeakerEntry(name, isWearer)}`. Seeded from
`GET /speakers` on app start + after a rename/reassign; updated by every
transcript §E. Drives labels + the reassign picker. v1 in-memory (rebuilt
from `GET /speakers` after restart — no persistence needed because the
server is the source of truth).

### 7. `data/SpeakerApi.kt` — NEW
`GET /speakers` client (same `SenseHttpClient` pattern as `SessionApi`).
Resolves the current client per call (re-provision takes effect on the next
fetch).

### 8. You-confirmation
First time `SpeakerCache` observes a transcript with `is_wearer=true` and
`name == "You"` (the server's default wearer tag), show a one-time prompt
(a `SPEAKER_NUDGE`-style bubble or a small dialog) → on submit,
`NameSpeakerMsg{wearer_id, chosenName}`. Gated by a local "shown" flag
(session-scoped; re-shows after app restart if still "You").

## Data flows

- **Name**: server nudge (`proactive`+`propose={kind:"name_speaker",
  speaker_id}`) → `NameSpeakerBubble` → user types → `NameSpeakerMsg` →
  server `set_display_name` → later transcripts/`/speakers` carry the new name.
- **Rename**: tap label in Recordings → Rename dialog → `NameSpeakerMsg`
  (same message, existing `speaker_id`).
- **Reassign**: tap label → "Actually someone else…" → picker (from
  `SpeakerCache`, seeded by `GET /speakers`) → `ReassignSpeakerMsg{from, to,
  scope="all"}` → server `reassign_speaker` relabels events + de-poisons
  centroids → labels refresh on next transcript/`/speakers` poll.
- **You-confirm**: first `is_wearer`/"You" transcript → prompt →
  `NameSpeakerMsg{wearer_id, name}`.

## Wire shapes (reference)

Outbound §E (server → app), new/changed fields only:
- `transcript`: `+ speaker_name, + is_wearer`
- `proactive`: `propose` (already present on the wire, now parsed): `{"kind":
  "name_speaker", "speaker_id": "..."}`

Inbound §E (app → server), NEW:
- `name_speaker`: `{type, session_id, speaker_id, name}`
- `reassign_speaker`: `{type, session_id, from_speaker_id, to_speaker_id,
  scope: "one"|"range"|"all"}`

HTTP:
- `GET /speakers` → `{"speakers": [{speakerId, displayName, isWearer,
  enrollmentStatus, turnCount, firstSeen, updatedAt}, ...]}`
- `GET /sessions/{id}/events` → each event `+ speaker, speakerName, isWearer,
  speakerConfidence, speakerAssignment`

## Error handling

- **Send failure** (WS dropped mid-send): show an error state on the bubble,
  do not crash. The existing relay auto-reconnect path restores the link; the
  user can retry. (The `/agent` timeout crash fix `1246d5e` is the template:
  no wire error escapes to crash the app.)
- **No server ack** for `name_speaker`/`reassign_speaker` (the handlers return
  `[]`). The app applies optimistically and trusts; correctness via the next
  transcript / `GET /speakers` refresh. A failed send is surfaced on the
  bubble, not swallowed.
- **Unknown `propose.kind`**: ignore (forward-compat, matches the lenient
  parser). Unknown §E `type` continues to drop silently (unchanged).
- **`GET /speakers` failure**: cache stays empty; labels fall back to "?";
  reassign picker shows only speakers heard this run. Non-fatal.
- **Speaker disabled** (`SENSE_SPEAKER_ENABLED=false`): no speaker fields
  populated, no `SPEAKER_NUDGE` ever sent, labels show nothing, You-confirm
  never triggers. Zero behavior change vs today.

## Constraints (preserved verbatim — do not undo)

- **No raw audio is ever stored** — only embeddings + transcript text; speaker
  ID runs on live PCM only.
- **Model-agnostic, local-first** — never hardcode a model or dim;
  embeddings/centroids never leave the Mac unless `SENSE_SPEAKER_EMBED_BASE_URL`
  is explicitly set. `GET /speakers` excludes `centroid`/`embedding_model`/`dim`.
- **Additive-only** — no schema migration; existing 782 server tests + 268
  Android tests stay green. New §E fields are optional; unknown types still
  drop silently.
- **Off by default** — `SENSE_SPEAKER_ENABLED=false` → zero embed calls,
  `speaker=None` on every Transcript, no speaker UI on the app.
- **Speaker ID never blocks transcription** — on embed failure, hop
  transcribed with `speaker=None`.
- **Events/atoms store the stable `speaker_id` UUID**, never a display name.
- **Vector type is `list[float]`** end-to-end (unchanged).
- **Every sqlite store uses `check_same_thread=False + threading.Lock`.**
- **`run_gateway.py` is manually smoke-tested** (pytest doesn't import it).
- **VAD 2e5→5e4 + whisper hallucination filter already shipped — do not undo.**

## Testing

### Server (782 → +N)
- `TranscriptMsg` carries `speaker_name`/`is_wearer`; gateway resolves from
  registry; None/False when speaker is None.
- `_event_to_wire` carries `speakerName`/`isWearer`; rename reflects at read
  time (write event, rename, read → new name) without backfill.
- `GET /speakers` returns all speakers, excludes biometrics, 401 on bad
  token, empty list when no speakers.

### Android (268 → +N)
- `Messages.kt` parse: `Transcript.speakerName/isWearer`, `Proactive.propose`
  (NameSpeakerPropose + unknown-kind → null).
- `RelaySession` dispatch: `propose` → `SPEAKER_NUDGE` ChatMessage;
  `Transcript` → SpeakerCache upsert; `sendControl` serializes + sends.
- `NameSpeakerBubble`: submit → `NameSpeakerMsg` sent + optimistic collapse.
- `SpeakerCache`: seed from `GET /speakers`, upsert on transcript, lookup.
- `SessionDetail`: renders `speakerName`; tap → Rename / reassign menu →
  correct message sent.
- You-confirmation: triggers once on first `is_wearer`/"You" transcript;
  suppressed after naming.

## Out of scope / deferred

- `scope="one"`/`"range"` reassign (per-utterance / time-range) — v1 is
  `scope="all"` only.
- Persisting `SpeakerCache` across restart (v1 rebuilds from `GET /speakers`).
- A `/speakers` write/rename endpoint over HTTP — naming stays on the WS
  `name_speaker` control message (one path).
- Speaker labels inside the live chat (transcripts aren't shown in chat;
  labels surface in Recordings only).
- VAD-segment embedding (server-side refinement, separate effort).