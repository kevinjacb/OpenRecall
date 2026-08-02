# HTTP Speaker Rename/Reassign — Design

**Date:** 2026-08-02
**Status:** Spec (pending implementation plan)
**Scope:** Sub-project of the post-bring-up work list. Speaker recognition is
now working on real hardware; this closes the offline-correction UX gap.

## Problem

Rename and reassign are the two speaker-correction actions the user can take
from the Android UI. Today both are **WebSocket-only**:

- Android enqueues `name_speaker` / `reassign_speaker` JSON control frames onto
  `SpeakerControlPort` (a process-singleton `ConcurrentLinkedQueue`).
- `RelayService.sendPendingSpeakerControls` drains that queue **only while the
  WS socket is non-null**.
- So for a **historical / offline Recordings session** (no live relay), the
  frames are never sent — the correction silently does nothing.

The server side is fully implemented and tested:
`SpeakerRegistry.name` (`speaker_registry.py:232`), the free function
`reassign_speaker` (`speaker_registry.py:440`), and the WS handlers
`_on_name_speaker` / `_on_reassign_speaker` (`gateway/core.py:290-315`). The gap
is the **Android → server transport** for offline sessions.

## Decision (already made in brainstorming)

- **Transport: HTTP always.** Android calls new HTTP endpoints for every
  rename/reassign, regardless of whether the WS relay is live. Single source of
  truth (server persists), one code path, works for offline/historical sessions.
  The WS outbound control-frame path is **retired** (Approach A — full
  single-path).
- **Reassign scope: v1 whole-speaker only.** No per-row `scope=one/range`, no
  "mint new speaker from these rows." `reassign_speaker` moves all of a
  speaker's rows/embeddings to an *existing* target speaker. The reassign picker
  remains "existing speakers only" (useful once ≥2 speakers are minted on
  hardware, which is the case on the user's device).

## Design

### Server endpoints

Two new POST routes in `http/routes/speakers.py`, following the existing
action-verb convention (`POST /commands/{id}/ack`, `POST /agent`) rather than
PATCH. Both inherit bearer-token auth from `bearer_auth_middleware`
(`http/auth.py`) — handlers do not check `request["authorized"]` themselves,
same as `/agent`.

**`POST /speakers/{speaker_id}/rename`**
- Body: `{"name": "Alice"}` (camelCase, matching the `GET /speakers` response).
- Calls `registry.name(speaker_id, name)` (sets display_name + enrollment
  `confirmed`).
- Returns `200 {"speaker": <wire>}` using the existing `_speaker_to_wire`
  (no biometrics — `centroid`/`embedding_model`/`dim` never serialized).

**`POST /speakers/reassign`**
- Body: `{"fromSpeakerId": "...", "toSpeakerId": "...", "scope": "all"}`.
- Calls `reassign_speaker(registry, event_store, atom_store, from, to, scope)`.
- Returns `204 No Content`.

`reassign_speaker` needs `event_store` (`app["sense_event_store"]`) and
`atom_store` (`app["sense_atom_store"]`); both are already stashed on the app
by `build_app` (`http/app.py:62,69`). The HTTP app already wires these in
`run_gateway.py`.

Wire fields are camelCase throughout to match the existing `GET /speakers`
response (`speakerId`, `displayName`, `isWearer`, …) and the Android JSON
conventions.

### DTOs

Small frozen pydantic models (`extra="forbid"`) added alongside the existing
DTOs in `http/routes/dto.py` (or inline in `speakers.py` if more local), mirroring
`AgentRequestDTO`:

- `RenameSpeakerDTO(name: str)` — non-empty.
- `ReassignSpeakerDTO(fromSpeakerId: str, toSpeakerId: str, scope: str = "all")`.

### Error contract

| Status | Condition |
|--------|-----------|
| 401 | No/bad bearer token — handled by `bearer_auth_middleware` before the handler. |
| 400 | Missing/empty `name`; missing `fromSpeakerId`/`toSpeakerId`; `scope` not `"all"` (v1 rejects per-row explicitly rather than silently accepting-and-ignoring as the WS path did). |
| 404 | `speaker_id` (rename) or `from`/`to` (reassign) not in registry — `registry.name` / `reassign_speaker` raise `KeyError`, caught → 404. |
| 409 | `speaker_registry` is `None` (speaker recognition disabled) or `event_store`/`atom_store` not wired → `{"error":"speaker_recognition_disabled"}`. |

`registry is None` mirrors `list_speakers`'s "disabled" branch, but a mutation
under disabled mode is a conflict (409), not an empty list.

### Android transport (HTTP-backed `SpeakerActions`)

The seam the ViewModels use is the `SpeakerActions` interface
(`data/SpeakerActions.kt`). Today its production impl is `SpeakerControlPort`
(WS queue). We replace that with an HTTP-backed impl.

- **`SenseHttpClient`** gains `suspend fun renameSpeaker(speakerId, name):
  SpeakerDto` and `suspend fun reassignSpeaker(fromId, toId, scope)`.
  `req(path)` already adds `Authorization: Bearer $token`; the new methods
  follow the existing `getSpeakers()` / `getSession()` OkHttp pattern (POST JSON
  body, parse response / ignore 204 body).
- **`SpeakerApi` interface** gains the two suspend methods; `HttpSpeakerApi`
  delegates to the client; `SpeakerRepository` exposes
  `suspend fun renameSpeaker(...): SpeakerEntry` and
  `suspend fun reassignSpeaker(...)` (returns the updated entry / Unit). Tests
  inject a `FakeSpeakerApi` (the established faking pattern — `SenseHttpClient`
  is `final`).
- **New `HttpSpeakerActions : SpeakerActions`** wrapping `SpeakerRepository`,
  run on `Dispatchers.IO`.
- **`SpeakerActions` methods become `suspend`** (currently fire-and-forget
  `fun`). The two call sites — `SessionDetailViewModel.renameSpeaker` /
  `reassignSpeaker` and `ChatViewModel.nameSpeaker` — already wrap calls in
  `runCatching`; they move into `viewModelScope.launch { runCatching { … } }`.
- **DI** swaps `SpeakerControlPort` for `HttpSpeakerActions` (wired in
  `RepositoryModule`, the same place `SpeakerRepository.fromClient` is wired).

### Retiring the WS send path (Approach A)

- Remove `SpeakerControlPort` (the object + its queue).
- Remove `RelayService.sendPendingSpeakerControls` and the drain loop that
  calls it.
- Remove the `NameSpeakerMsg` / `ReassignSpeakerMsg` protocol message classes
  (Android no longer encodes them).
- Remove the server WS handlers `_on_name_speaker` / `_on_reassign_speaker`
  (`gateway/core.py:290-315`) and their dispatch entries, plus the
  `NameSpeaker` / `ReassignSpeaker` WS message types and their tests.

**Stays untouched:** the inbound *nudge* path — `SpeakerNudgeListener`, the
`NameSpeakerBubble`, and the one-time You-confirmation prompt. That path is
server→Android `propose name_speaker` (a different direction); it just
dispatches its user response through the now-HTTP `SpeakerActions` instead of
the WS control frame.

### Refresh-after-action (making the correction visible)

`GET /sessions/{id}/events` resolves `speakerName` / `isWearer` from the
registry **at read time** (`http/routes/sessions.py:94`:
`registry.get(e.speaker)`). This makes the refresh design clean:

- **Rename:** keep the existing **optimistic** `speakerCache.upsert(speakerId,
  name, isWearer)` so the label updates instantly, then persist via HTTP. On
  HTTP **failure**: revert the optimistic cache entry to its prior value and
  surface a transient error through the existing `Outcome`/error channel the
  VMs already use — *not* a crash, *not* a silent buffer (the old WS port
  buffered; HTTP cannot buffer across restarts, so failure must be visible).
- **Reassign:** no optimistic local relabel (v1 relabels the whole speaker
  across all sessions — too easy to get wrong locally and out of sync). On
  HTTP **success**, call the VM's existing `onRefresh()` → bumps `revision` →
  `flatMapLatest` re-fetches the session events → the relabeled `speaker_id`
  rows resolve to the to-speaker's `display_name` server-side → timeline
  re-renders with the correct name. `SpeakerCache` is unaffected (the set of
  speakers doesn't change).

### Out of scope

- Per-row `scope=one/range` reassign and "mint new speaker from these rows"
  (deferred; needs per-row relabel in `EventStore`/`AtomStore` + per-row
  embedding move + Android "create new speaker" UI).
- Any change to `reassign_speaker` v1 whole-speaker semantics.
- Speaker threshold tuning / VAD-segment embedding (separately deferred —
  speaker recognition is working on hardware).

## Constraints preserved

Off by default (`SENSE_SPEAKER_ENABLED`); model-agnostic; no raw audio stored;
`list[float]` end-to-end; embeddings/centroids never leave the Mac unless
`SENSE_SPEAKER_EMBED_BASE_URL` set; `GET /speakers` (and the new POSTs) exclude
centroid/embedding_model/dim; speaker ID never blocks transcription; events/
atoms store stable `speaker_id` UUID, names resolved at read time; every sqlite
store `check_same_thread=False` + `Lock`; additive-only, no schema migration;
existing tests stay green. See the speaker-recognition memories.

## Testing

### Server
Extend the speaker route tests (`tests/http/` or wherever `GET /speakers` tests
live):
- 401 — no/bad bearer token.
- 400 — empty `name`; missing `fromSpeakerId`/`toSpeakerId`; `scope != "all"`.
- 404 — unknown `speaker_id` (rename); unknown `from`/`to` (reassign).
- 409 — `speaker_registry` is `None`; `event_store`/`atom_store` not wired.
- 200 rename — returns the updated speaker, biometrics excluded, enrollment
  `confirmed`.
- 204 reassign — seed events + atoms under `from` across two sessions, call
  reassign, assert every event/atom row is relabeled to `to`,
  `from`'s ring-buffer embeddings moved into `to`, and both centroids
  recomputed.

### Android
- `FakeSpeakerApi` gains the two suspend methods; `HttpSpeakerActions` test
  delegates to the repository.
- `SessionDetailViewModel` test — rename optimistically upserts the cache then
  persists; HTTP failure reverts the cache entry and surfaces an error;
  reassign calls the repo and, on success, bumps `onRefresh` (re-fetch).
- `ChatViewModel.nameSpeaker` test updated for the suspend signature.
- Delete the `SpeakerControlPort` / `RelayService.sendPendingSpeakerControls`
  drain tests.

## Open detail to confirm during planning

- Exact placement of the new DTOs (`dto.py` vs inline in `speakers.py`) — match
  wherever the speaker route's existing DTOs (if any) live.
- The `SpeakerActions` interface keeps the `sessionId` parameter (call sites
  unchanged); the HTTP impl ignores it (v1 reassign is global) or forwards it
  as an optional query param for server-side logging. Decide during planning.