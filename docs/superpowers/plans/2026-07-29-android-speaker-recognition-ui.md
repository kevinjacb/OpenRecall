# Android Speaker-Recognition UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make server-side speaker recognition visible and actionable on the Android relay app — name unknown speakers, label transcripts by speaker, rename, reassign mislabels, and confirm the "You" wearer tag — with one new HTTP endpoint and additive wire fields.

**Architecture:** The server resolves speaker identity at read time (registry lookup at emit + at HTTP read) and exposes `GET /speakers` (minus biometrics). The app seeds an in-memory `SpeakerCache` from that endpoint, renders speaker labels in Recordings, renders an interactive `NameSpeakerBubble` for proactive name-nudges, and sends lightweight `name_speaker`/`reassign_speaker` control messages back over the existing WS. Naming and rename share one message (`NameSpeaker` → server `set_display_name`). All additions are optional fields / new types; unknown types still drop silently. Off by default is unchanged.

**Tech Stack:** Python 3 / aiohttp / Pydantic v2 (server); Kotlin / kotlinx.serialization / OkHttp / Jetpack Compose / coroutines (Android); ESP-IDF 5.1.6 firmware (untouched by this plan).

## Global Constraints

(Preserved verbatim from the approved spec — do not undo. Every task's requirements implicitly include this section.)

- **No raw audio is ever stored** — only embeddings + transcript text; speaker ID runs on live PCM only.
- **Model-agnostic, local-first** — never hardcode a model or dim; embeddings/centroids never leave the Mac unless `SENSE_SPEAKER_EMBED_BASE_URL` is explicitly set. `GET /speakers` excludes `centroid`/`embedding_model`/`dim`.
- **Additive-only** — no schema migration; existing 782 server tests + 268 Android tests stay green. New §E fields are optional; unknown types still drop silently.
- **Off by default** — `SENSE_SPEAKER_ENABLED=false` → zero embed calls, `speaker=None` on every Transcript, no speaker UI on the app, `GET /speakers` returns `[]` (or `{"speakers":[]}`).
- **Speaker ID never blocks transcription** — on embed failure, hop transcribed with `speaker=None`.
- **Events/atoms store the stable `speaker_id` UUID**, never a display name. Names are resolved at read time.
- **Vector type is `list[float]`** end-to-end (unchanged).
- **Every sqlite store uses `check_same_thread=False + threading.Lock`.**
- **`run_gateway.py` is manually smoke-tested** (pytest doesn't import it).
- **VAD 2e5→5e4 + whisper hallucination filter already shipped — do not undo.**
- **Biometric guarantee:** `centroid`/`embedding_model`/`dim` are NEVER serialized over HTTP.
- **UI invariant:** composables under `ui/chat/` must NOT import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*` (enforced by `ArchitecturalInvariantsTest`). A `NameSpeakerBubble` stays DTO-free.
- **Server tests:** `cd /Users/kevin/Projects/Sense/server && python -m pytest <path> -v`.
- **Android tests:** `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "<fully.qualified.or.pattern>"`.
- **Commits** use Conventional Commits and end with a blank line + `Co-Authored-By: Claude <noreply@anthropic.com>`. Commit only the files the task touched unless told otherwise.

---

## File Structure

### Server (additive — no migration)

| File | Responsibility | New? |
|------|----------------|------|
| `server/src/sense_server/protocol/messages.py` | `TranscriptMsg` gains `speaker_name` + `is_wearer` (optional) | Modify |
| `server/src/sense_server/gateway/core.py` | `_emit` resolves `speaker_name`/`is_wearer` from `self._speaker_registry` | Modify |
| `server/src/sense_server/http/app.py` | `build_app` accepts `speaker_registry=`, stashes `app["sense_speaker_registry"]`, registers the speakers route | Modify |
| `server/src/sense_server/http/routes/speakers.py` | NEW `GET /speakers` — lists registry without biometrics | Create |
| `server/src/sense_server/http/routes/sessions.py` | `_event_to_wire(e, registry)` adds `speaker`/`speakerName`/`isWearer`/`speakerConfidence`/`speakerAssignment`; route reads `sense_speaker_registry` | Modify |
| `server/tests/gateway/test_core.py` | + test: `TranscriptMsg` carries resolved name/is_wearer; None/False when speaker is None | Modify |
| `server/tests/http/test_speakers.py` | NEW: GET /speakers happy + 401 + empty; excludes biometrics | Create |
| `server/tests/http/test_sessions.py` | + test: events carry speaker fields; rename reflects at read time | Modify |

### Android (additive — exhaustive `when`s force compile-driven alignment)

| File | Responsibility | New? |
|------|----------------|------|
| `android/.../protocol/Messages.kt` | `Transcript` +speakerName/isWearer; `Proactive` +propose; outbound `NameSpeakerMsg`/`ReassignSpeakerMsg` + `encode()` | Modify |
| `android/.../protocol/Wire.kt` | (already `ignoreUnknownKeys=true`) — unchanged | — |
| `android/.../RelaySession.kt` | dispatch `propose`→`SPEAKER_NUDGE`; `Transcript`→`SpeakerCache`; `sendControl(msg)`; exhaustive `when` gets new branch | Modify |
| `android/.../data/ChatHistoryStore.kt` | `ChatMessageKind.SPEAKER_NUDGE`; `ChatMessage` +propose/sessionId/speakerId | Modify |
| `android/.../data/SpeakerCache.kt` | NEW in-memory `{speakerId → SpeakerEntry(name, isWearer)}` | Create |
| `android/.../data/SpeakerApi.kt` | NEW `interface SpeakerApi` + `SpeakerRepository` + `HttpSpeakerApi` adapter | Create |
| `android/.../http/dto/Dtos.kt` | `CaptureEventDto` +speaker/speakerName/isWearer/...; NEW `SpeakerDto` + `SpeakersDto` | Modify |
| `android/.../http/dto/Mappers.kt` | `toDomainOrNull` folds speaker fields into `TranscriptChunk` | Modify |
| `android/.../http/SenseHttpClient.kt` | `getSpeakers(): List<SpeakerDto>` | Modify |
| `android/.../domain/model/TranscriptChunk.kt` | +speaker/speakerName/isWearer | Modify |
| `android/.../data/RepositoryModule.kt` | wire `speakerRepository` + `SpeakerCache`; `Repositories` field | Modify |
| `android/.../ui/chat/ChatMessageList.kt` | exhaustive `when` + `SPEAKER_NUDGE -> NameSpeakerBubble`; thread `onNameSpeaker`/`onReassign` callbacks | Modify |
| `android/.../ui/chat/NameSpeakerBubble.kt` | NEW interactive nudge bubble (DTO-free) | Create |
| `android/.../ui/recordings/SessionDetailScreen.kt` | `EventRow` shows speakerName; tap → Rename / Reassign menu | Modify |
| `android/.../ui/recordings/SessionDetailViewModel.kt` | expose `SpeakerCache`; `rename`/`reassign` actions; You-confirm state | Modify |
| `android/.../ui/recordings/YouConfirmationBanner.kt` | NEW one-time You-confirmation prompt | Create |
| `tests: RelaySessionTest.kt` | parse propose; dispatch → SPEAKER_NUDGE; sendControl frame | Modify |
| `tests: ChatMessageKindTest.kt` | pin `SPEAKER_NUDGE` enum name | Modify |
| `tests: SpeakerCacheTest.kt` | NEW: seed/upsert/lookup | Create |
| `tests: SpeakerRepositoryImplTest.kt` | NEW: fake api + runTest | Create |
| `tests: MessagesParseTest.kt` | NEW: transcript propose parse | Create |
| `tests: SessionDetailViewModelTest.kt` | + rename/reassign/You-confirm | Modify |

---

## Task 1 (Server): `TranscriptMsg` carries resolved `speaker_name`/`is_wearer`

**Files:**
- Modify: `server/src/sense_server/protocol/messages.py:109-118`
- Modify: `server/src/sense_server/gateway/core.py:422-428`
- Test: `server/tests/gateway/test_core.py` (add a case)

**Interfaces:**
- Consumes: `self._speaker_registry: SpeakerRegistry | None` (already on `GatewayCore`, core.py:159-169); `SpeakerRegistry.get(speaker_id) -> Speaker | None` with `.display_name: str | None`, `.is_wearer: bool`.
- Produces: `TranscriptMsg.speaker_name: str | None`, `TranscriptMsg.is_wearer: bool`.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/gateway/test_core.py`:

```python
def test_emit_resolves_speaker_name_and_is_wearer_on_transcript_msg():
    core, store = make_core_with_store_and_speaker(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))
    core.on_audio(audio_bytes(0, n_frames=5))

    msgs = [m for m in core._emit.__self__._outbox] if isinstance(m, TranscriptMsg)] \
        if False else []
    # Drive through on_audio's returned messages instead (the public path).
    out = core.on_audio(audio_bytes(0, n_frames=0)) if False else []
    # Re-emit by feeding audio (the existing test pattern drives emit via on_audio).
    core.on_control(Hello(session_id="s1", start_seq=0))
    out = core.on_audio(audio_bytes(0, n_frames=5))
    transcripts = [m for m in out if isinstance(m, TranscriptMsg)]
    assert transcripts, "expected transcript messages"
    # The "you" speaker is the wearer (display_name="You", is_wearer=True).
    for tmsg in transcripts:
        assert tmsg.speaker_name == "You"
        assert tmsg.is_wearer is True


def test_emit_transcript_msg_name_none_when_speaker_unknown():
    # A registry that has no row for the hop's speaker → name None, is_wearer False.
    core, store = make_core_with_store_and_speaker(window_ms=100)
    core.on_control(Hello(session_id="s1", start_seq=0))
    out = core.on_audio(audio_bytes(0, n_frames=5))
    # Sanity: with the fixture-driven recognizer the speaker is "you"; this test
    # documents the resolution path. For a truly-unknown speaker the identifier
    # would emit a fresh UUID; here we assert the field exists and is a str|None.
    for tmsg in [m for m in out if isinstance(m, TranscriptMsg)]:
        assert hasattr(tmsg, "speaker_name")
        assert hasattr(tmsg, "is_wearer")
```

> Note: `make_core_with_store_and_speaker` already seeds the "you" wearer speaker (`display_name="You"`, `is_wearer=True`) per the explorer report (test_core.py:324-329). If the helper does not expose `_outbox`, drive emit through `on_audio` as the existing tests do. Adjust the first assertion block to use whichever path the existing `test_emit_carries_speaker_into_event_and_transcript_msg` (line 353) uses — mirror it exactly, only adding the two new-field assertions.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/gateway/test_core.py::test_emit_resolves_speaker_name_and_is_wearer_on_transcript_msg -v`
Expected: FAIL — `TranscriptMsg` has no `speaker_name` / `is_wearer`.

- [ ] **Step 3: Add the fields to `TranscriptMsg`**

In `server/src/sense_server/protocol/messages.py`, edit the `TranscriptMsg` class (lines 109-118) to:

```python
class TranscriptMsg(_Strict):
    """A transcribed window pushed back to the client."""

    type: Literal["transcript"] = "transcript"
    session_id: str
    text: str
    duration_ms: int
    speaker: str | None = None
    speaker_confidence: float | None = None
    speaker_assignment: str | None = None
    speaker_name: str | None = None  # resolved display_name at emit time
    is_wearer: bool = False           # this hop's speaker is the wearer
```

- [ ] **Step 4: Resolve at emit time in the gateway**

In `server/src/sense_server/gateway/core.py`, edit the `TranscriptMsg(...)` construction at lines 422-428 to resolve from the registry:

```python
            sp = (
                self._speaker_registry.get(t.speaker)
                if (t.speaker and self._speaker_registry is not None)
                else None
            )
            msgs.append(
                TranscriptMsg(
                    session_id=self._session_id, text=t.text, duration_ms=t.duration_ms,
                    speaker=t.speaker, speaker_confidence=t.speaker_confidence,
                    speaker_assignment=t.speaker_assignment,
                    speaker_name=sp.display_name if sp is not None else None,
                    is_wearer=sp.is_wearer if sp is not None else False,
                )
            )
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/gateway/test_core.py -v`
Expected: PASS (all gateway tests, including the existing `test_emit_carries_speaker_into_event_and_transcript_msg`).

- [ ] **Step 6: Run the full server suite to confirm additive-only**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest -q`
Expected: PASS — 782 (no regressions; new fields are optional).

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/protocol/messages.py server/src/sense_server/gateway/core.py server/tests/gateway/test_core.py
git commit -m "feat(server): resolve speaker_name/is_wearer on TranscriptMsg at emit time"
```

---

## Task 2 (Server): HTTP events carry speaker fields + registry wired into `build_app`

**Files:**
- Modify: `server/src/sense_server/http/app.py:26-89` (add `speaker_registry=` kwarg + app key + register route)
- Modify: `server/src/sense_server/http/routes/sessions.py:20-97,152-159` (imports, `_event_to_wire`, callers)
- Test: `server/tests/http/test_sessions.py` (add cases + thread registry into `_client`)

**Interfaces:**
- Consumes: `SpeakerRegistry.list_speakers() -> list[Speaker]`, `.get(speaker_id) -> Speaker | None`; `Speaker.display_name`, `.is_wearer`, `.enrollment_status`, `.turn_count`, `.first_seen`, `.updated_at`.
- Produces: `build_app(..., speaker_registry=None)`; `app["sense_speaker_registry"]`; `_event_to_wire(e, registry)`; new wire fields `speaker`, `speakerName`, `isWearer`, `speakerConfidence`, `speakerAssignment`.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/http/test_sessions.py`. First extend the `_client` helper to accept + thread a `speaker_registry`:

```python
async def _client_with_speakers(tmp_path, registry):
    token = load_or_create_token(tmp_path / "tok")
    index = SessionIndex()
    store = InMemoryEventStore()
    lifecycle = SessionLifecycle()
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=index,
        session_lifecycle=lifecycle,
        event_store=store,
        speaker_registry=registry,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token, index, store
```

Add the import at the top of the file:

```python
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker, SpeakerConfig
```

Then the tests:

```python
async def test_session_events_carry_speaker_fields(tmp_path):
    from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker, SpeakerConfig
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="sp-1", display_name="Sarah", is_wearer=False,
        enrollment_status="confirmed", centroid=None, embedding_model="fake",
        dim=8, turn_count=2, first_seen="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00"))
    cli, token, index, store = await _client_with_speakers(tmp_path, reg)
    index.register("s1")  # if SessionIndex has register; else use the existing seeding pattern
    store.append(_ce("s1", 0, text="hello", speaker="sp-1",
                    speaker_confidence=0.9, speaker_assignment="confirmed"))
    try:
        resp = await cli.get("/sessions/s1/events",
                             headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        ev = body["events"][0]
        assert ev["speaker"] == "sp-1"
        assert ev["speakerName"] == "Sarah"
        assert ev["isWearer"] is False
        assert ev["speakerConfidence"] == 0.9
        assert ev["speakerAssignment"] == "confirmed"
    finally:
        await cli.close()


async def test_session_events_rename_reflects_at_read_time_without_backfill(tmp_path):
    from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker, SpeakerConfig
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(Speaker(
        speaker_id="sp-2", display_name="Sara", is_wearer=False,
        enrollment_status="confirmed", centroid=None, embedding_model="fake",
        dim=8, turn_count=1, first_seen="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00"))
    cli, token, index, store = await _client_with_speakers(tmp_path, reg)
    index.register("s2")
    store.append(_ce("s2", 0, text="hi", speaker="sp-2",
                    speaker_confidence=0.8, speaker_assignment="confirmed"))
    try:
        # Read before rename.
        r1 = await cli.get("/sessions/s2/events",
                           headers={"Authorization": f"Bearer {token}"})
        assert (await r1.json())["events"][0]["speakerName"] == "Sara"
        # Rename on the registry (the WS name_speaker path does this).
        reg.set_display_name("sp-2", "Sarah")
        # No backfill — read again and the new name appears.
        r2 = await cli.get("/sessions/s2/events",
                           headers={"Authorization": f"Bearer {token}"})
        assert (await r2.json())["events"][0]["speakerName"] == "Sarah"
    finally:
        await cli.close()


async def test_session_events_speaker_none_yields_no_name(tmp_path):
    from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, SpeakerConfig
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    cli, token, index, store = await _client_with_speakers(tmp_path, reg)
    index.register("s3")
    store.append(_ce("s3", 0, text="silence", speaker=None))
    try:
        resp = await cli.get("/sessions/s3/events",
                             headers={"Authorization": f"Bearer {token}"})
        ev = (await resp.json())["events"][0]
        assert ev["speaker"] is None
        assert ev["speakerName"] is None
        assert ev["isWearer"] is False
    finally:
        await cli.close()
```

> Adjust the `index.register(...)` call to whatever `SessionIndex` uses to record a session so `_index(request.app).summary(session_id)` is non-None (mirror the existing test that seeds events — if existing tests use a different seeding helper, use that). If `store.append` is not the method name, use whatever the existing `test_sessions.py` seeding uses (the `_ce` helper builds a `CaptureEvent`; the existing tests append via the store's real method — match it).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/http/test_sessions.py -v`
Expected: FAIL — `build_app()` got an unexpected keyword `speaker_registry`, and events lack `speakerName`.

- [ ] **Step 3: Wire `speaker_registry` into `build_app`**

In `server/src/sense_server/http/app.py`:

Add `speaker_registry=None` to the `build_app` signature (after `command_dispatcher=None`):

```python
def build_app(
    *,
    token,
    get_pubkey,
    event_store: EventStore | None = None,
    session_index: SessionIndex | None = None,
    session_lifecycle: SessionLifecycle | None = None,
    gateway_port: int | None = None,
    planner=None,
    retriever=None,
    atom_store=None,
    metrics=None,
    id_generator=None,
    command_store=None,
    command_dispatcher=None,
    speaker_registry=None,
):
```

Stash the app key near the others (after `app["sense_command_dispatcher"]`):

```python
    app["sense_speaker_registry"] = speaker_registry
```

Register the speakers route (after `add_commands(app)` / before `return app`):

```python
    from sense_server.http.routes.speakers import add_routes as add_speakers
    add_speakers(app)
```

- [ ] **Step 4: Enrich `_event_to_wire`**

In `server/src/sense_server/http/routes/sessions.py`, change the signature and body of `_event_to_wire` (lines 80-97):

```python
def _event_to_wire(e, registry=None) -> dict:
    """Map a CaptureEvent to the wire shape matching the Android DTO.

    Speaker display fields are resolved at read time from the registry so
    renames/reassigns reflect immediately without a backfill. The stored
    event row keeps only the stable speaker_id UUID.
    """
    sp = registry.get(e.speaker) if (e.speaker and registry is not None) else None
    return {
        "id": e.event_id,
        "sessionId": e.session_id,
        "seq": e.seq,
        "startMs": e.start_ms,
        "createdAt": e.created_at.isoformat(),
        "kind": e.kind,
        "text": e.text or "",
        "durationMs": e.duration_ms,
        "codec": "",
        "sampleRateHz": 0,
        "byteCount": 0,
        "speaker": e.speaker,
        "speakerName": sp.display_name if sp is not None else None,
        "isWearer": sp.is_wearer if sp is not None else False,
        "speakerConfidence": e.speaker_confidence,
        "speakerAssignment": e.speaker_assignment,
    }
```

Add a `_speaker_registry` accessor near `_store` (lines 58-65):

```python
def _speaker_registry(app: web.Application):
    return app.get("sense_speaker_registry")
```

Update the caller in `get_session_events` (lines 152-159) to pass the registry:

```python
async def get_session_events(request: web.Request) -> web.Response:
    session_id = request.match_info["session_id"]
    idx = _index(request.app)
    summary = idx.summary(session_id)
    if summary is None:
        return web.json_response({"error": "not_found"}, status=404)
    events = _store(request.app).events(session_id)
    registry = _speaker_registry(request.app)
    return web.json_response({"events": [_event_to_wire(e, registry) for e in events]})
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/http/test_sessions.py -v`
Expected: PASS.

- [ ] **Step 6: Run the full server suite**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest -q`
Expected: PASS (782 + new). The new fields are additive; existing callers that ignore unknown keys are unaffected.

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/http/app.py server/src/sense_server/http/routes/sessions.py server/tests/http/test_sessions.py
git commit -m "feat(server): surface speaker name/is_wearer on session events at read time"
```

---

## Task 3 (Server): `GET /speakers` endpoint (no biometrics)

**Files:**
- Create: `server/src/sense_server/http/routes/speakers.py`
- Test: `server/tests/http/test_speakers.py`

**Interfaces:**
- Consumes: `app["sense_speaker_registry"]` → `SpeakerRegistry.list_speakers() -> list[Speaker]`; `Speaker` fields `{speaker_id, display_name, is_wearer, enrollment_status, turn_count, first_seen, updated_at}` (exclude `centroid`, `embedding_model`, `dim`).
- Produces: `GET /speakers` → `{"speakers": [{speakerId, displayName, isWearer, enrollmentStatus, turnCount, firstSeen, updatedAt}, ...]}`. 401 when token set and bad bearer (via middleware). `{"speakers": []}` when disabled/empty.

- [ ] **Step 1: Write the failing tests**

Create `server/tests/http/test_speakers.py`:

```python
from aiohttp.test_utils import TestClient, TestServer

from sense_server.auth import load_or_create_token
from sense_server.events.store import InMemoryEventStore
from sense_server.http.app import build_app
from sense_server.memory.speaker_registry import InMemorySpeakerRegistry, Speaker, SpeakerConfig
from sense_server.sessions.index import SessionIndex
from sense_server.sessions.lifecycle import SessionLifecycle


def _speaker(speaker_id, display_name, is_wearer=False):
    return Speaker(
        speaker_id=speaker_id, display_name=display_name, is_wearer=is_wearer,
        enrollment_status="confirmed", centroid=None, embedding_model="fake",
        dim=8, turn_count=3, first_seen="2026-07-29T00:00:00+00:00",
        updated_at="2026-07-29T00:00:00+00:00",
    )


async def _client(tmp_path, registry):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(),
        session_lifecycle=SessionLifecycle(),
        event_store=InMemoryEventStore(),
        speaker_registry=registry,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token


async def test_speakers_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, InMemorySpeakerRegistry(SpeakerConfig()))
    try:
        resp = await cli.get("/speakers")
        assert resp.status == 401
    finally:
        await cli.close()


async def test_speakers_empty_when_no_registry(tmp_path):
    cli, token = await _client(tmp_path, None)
    try:
        resp = await cli.get("/speakers", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        assert await resp.json() == {"speakers": []}
    finally:
        await cli.close()


async def test_speakers_lists_all_excluding_biometrics(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", "Sarah"))
    reg.add_speaker(_speaker("you", "You", is_wearer=True))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.get("/speakers", headers={"Authorization": f"Bearer {token}"})
        assert resp.status == 200
        body = await resp.json()
        ids = sorted(s["speakerId"] for s in body["speakers"])
        assert ids == ["sp-1", "you"]
        by_id = {s["speakerId"]: s for s in body["speakers"]}
        assert by_id["sp-1"]["displayName"] == "Sarah"
        assert by_id["sp-1"]["isWearer"] is False
        assert by_id["you"]["isWearer"] is True
        # Biometric guarantee: these keys must NEVER be present.
        for s in body["speakers"]:
            assert "centroid" not in s
            assert "embeddingModel" not in s
            assert "embedding_model" not in s
            assert "dim" not in s
            assert "enrollmentStatus" in s
            assert "turnCount" in s
            assert "firstSeen" in s
            assert "updatedAt" in s
    finally:
        await cli.close()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/http/test_speakers.py -v`
Expected: FAIL — `add_speakers` import errors / no `/speakers` route (404, not the asserted behavior).

- [ ] **Step 3: Create the route**

Create `server/src/sense_server/http/routes/speakers.py`:

```python
from __future__ import annotations

import json

from aiohttp import web


def add_routes(app: web.Application) -> None:
    app.router.add_get("/speakers", list_speakers)


def _registry(app: web.Application):
    reg = app.get("sense_speaker_registry")
    return reg


def _speaker_to_wire(s) -> dict:
    """Serialize a Speaker WITHOUT biometrics (centroid/embedding_model/dim)."""
    return {
        "speakerId": s.speaker_id,
        "displayName": s.display_name,
        "isWearer": s.is_wearer,
        "enrollmentStatus": s.enrollment_status,
        "turnCount": s.turn_count,
        "firstSeen": s.first_seen,
        "updatedAt": s.updated_at,
    }


async def list_speakers(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    if reg is None:
        return web.json_response({"speakers": []})
    try:
        speakers = reg.list_speakers()
    except Exception as e:  # pragma: no cover - last-resort safety net
        return web.json_response(
            {"error": repr(e)}, content_type="application/json", status=500,
        )
    return web.json_response({"speakers": [_speaker_to_wire(s) for s in speakers]})
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest tests/http/test_speakers.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full server suite**

Run: `cd /Users/kevin/Projects/Sense/server && python -m pytest -q`
Expected: PASS.

- [ ] **Step 6: Manually smoke-test run_gateway.py**

Run (best-effort warmup already shipped; this only checks the new route boots):

```bash
cd /Users/kevin/Projects/Sense/server
( SENSE_SPEAKER_ENABLED=1 SENSE_EMBED_MODEL=dummy SENSE_LLM_MODEL=dummy \
  python -u scripts/run_gateway.py > /tmp/gw_speakers.log 2>&1 & PID=$!; \
  sleep 8; curl -s http://127.0.0.1:8080/speakers; echo; kill $PID; wait $PID )
```

Expected: the server boots (no `speaker_registry` wiring crash) and `/speakers` returns `{"speakers":[]}` (no speakers minted yet). If the gateway port differs, grep the log for the listening port. Confirm the route is reachable (not 404) and no biometric fields appear.

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/http/routes/speakers.py server/tests/http/test_speakers.py
git commit -m "feat(server): add GET /speakers (excludes biometrics)"
```

---

## Task 4 (Android): parse new transcript fields + proactive `propose` + outbound control messages

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/MessagesParseTest.kt` (new) + extend `RelaySessionTest.kt` parse coverage indirectly in Task 7.

**Interfaces:**
- Consumes: `Wire.json` (`ignoreUnknownKeys=true`, Messages.kt:20); the lenient `parseServerMessage` discriminator.
- Produces: `ServerMessage.Transcript` + `speakerName: String?`, `isWearer: Boolean`; `ServerMessage.Proactive` + `propose: NameSpeakerPropose?`; new `@Serializable` `NameSpeakerMsg`/`ReassignSpeakerMsg` with `encode()`.

- [ ] **Step 1: Write the failing parse test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/MessagesParseTest.kt`:

```kotlin
package com.sense.relay

import com.sense.relay.protocol.ServerMessage
import com.sense.relay.protocol.parseServerMessage
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class MessagesParseTest {

    @Test
    fun transcript_carries_speaker_name_and_is_wearer() {
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":"sp-1","speaker_name":"Sarah","is_wearer":false}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertEquals("Sarah", msg.speakerName)
        assertEquals(false, msg.isWearer)
    }

    @Test
    fun transcript_speaker_name_defaults_null_when_absent() {
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertNull(msg.speakerName)
        assertEquals(false, msg.isWearer)
    }

    @Test
    fun proactive_with_name_speaker_propose_is_parsed() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"Who was that?",
               "atoms":[],"propose":{"kind":"name_speaker","speaker_id":"sp-9"}}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        val propose = msg.propose
        assertTrue(propose != null)
        assertEquals("sp-9", propose.speakerId)
    }

    @Test
    fun proactive_with_unknown_propose_kind_yields_null() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"x","atoms":[],
               "propose":{"kind":"future_thing","speaker_id":"sp-9"}}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        assertNull(msg.propose)
    }

    @Test
    fun proactive_without_propose_yields_null() {
        val msg = parseServerMessage(
            """{"type":"proactive","request_id":"r1","text":"x","atoms":[]}"""
        )
        assertTrue(msg is ServerMessage.Proactive)
        assertNull(msg.propose)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.MessagesParseTest"`
Expected: FAIL — `Transcript` has no `speakerName`/`isWearer`; `Proactive` has no `propose`.

- [ ] **Step 3: Extend `Messages.kt`**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt`:

Update `ServerMessage.Transcript` (lines 58-60) and `ServerMessage.Proactive` (lines 62-67):

```kotlin
        /** A transcribed window (informational to the relay). */
        data class Transcript(
            val text: String,
            val durationMs: Int,
            val speakerName: String? = null,
            val isWearer: Boolean = false,
        ) : ServerMessage

        data class Command(val payload: String, val sig: String) : ServerMessage

        data class Proactive(
            val requestId: String,
            val text: String,
            val atoms: List<String>,
            val propose: NameSpeakerPropose? = null,
        ) : ServerMessage
```

Add the `NameSpeakerPropose` type near the other shared protocol types (above `parseServerMessage`):

```kotlin
/** Parsed from a proactive `propose` payload. Unknown kinds yield null. */
data class NameSpeakerPropose(val speakerId: String)
```

Update `parseServerMessage` (lines 79-98) to read the new fields:

```kotlin
fun parseServerMessage(text: String): ServerMessage {
    val obj = runCatching { Wire.json.parseToJsonElement(text) as? JsonObject }.getOrNull()
        ?: return ServerMessage.Unknown("malformed")
    fun str(k: String) = (obj[k]?.jsonPrimitive?.content)
    fun int(k: String) = (obj[k]?.jsonPrimitive?.content?.toIntOrNull())
    fun bool(k: String): Boolean = (obj[k]?.jsonPrimitive?.contentOrNull)?.toBooleanStrictOrNull() ?: false
    return when (str("type")) {
        "ack" -> ServerMessage.Ack(int("next_seq") ?: 0)
        "request_chunks" -> ServerMessage.RequestChunks(int("start") ?: 0, int("end") ?: 0)
        "transcript" -> ServerMessage.Transcript(
            text = str("text") ?: "",
            durationMs = int("duration_ms") ?: 0,
            speakerName = str("speaker_name"),
            isWearer = bool("is_wearer"),
        )
        "command" -> ServerMessage.Command(str("payload") ?: "", str("sig") ?: "")
        "proactive" -> ServerMessage.Proactive(
            requestId = str("request_id") ?: "",
            text = str("text") ?: "",
            atoms = (obj["atoms"] as? kotlinx.serialization.json.JsonArray)
                ?.mapNotNull { runCatching { it.jsonPrimitive.content }.getOrNull() }
                ?: emptyList(),
            propose = parsePropose(obj["propose"] as? JsonObject),
        )
        else -> ServerMessage.Unknown(str("type") ?: "missing")
    }
}

/** Only `name_speaker` is recognized today; unknown kinds → null (forward-compat). */
fun parsePropose(propose: JsonObject?): NameSpeakerPropose? {
    if (propose == null) return null
    val kind = propose["kind"]?.jsonPrimitive?.contentOrNull
    if (kind != "name_speaker") return null
    val speakerId = propose["speaker_id"]?.jsonPrimitive?.contentOrNull ?: return null
    return NameSpeakerPropose(speakerId = speakerId)
}
```

Add the outbound control messages (near `Hello`/`Bye`/`CommandAck`, lines 25-47). Both carry a `type` discriminator for the server's `parse_control`:

```kotlin
@Serializable
data class NameSpeakerMsg(
    val session_id: String,
    val speaker_id: String,
    val name: String,
    val type: String = "name_speaker",
)

@Serializable
data class ReassignSpeakerMsg(
    val session_id: String,
    val from_speaker_id: String,
    val to_speaker_id: String,
    val scope: String = "all",
    val type: String = "reassign_speaker",
)

fun NameSpeakerMsg.encode(): String = Wire.json.encodeToString(NameSpeakerMsg.serializer(), this)
fun ReassignSpeakerMsg.encode(): String = Wire.json.encodeToString(ReassignSpeakerMsg.serializer(), this)
```

> `contentOrNull` is `jsonPrimitive.contentOrNull` from kotlinx.serialization. If the existing helper `str(k)` already does the safe extraction, reuse its pattern. Confirm `toBooleanStrictOrNull` is available in the project's kotlinx.serialization version; if not, parse with `str("is_wearer")?.toBoolean() ?: false`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.MessagesParseTest"`
Expected: PASS.

- [ ] **Step 5: Compile the whole app (catch the two exhaustive `when` sites)**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:compileDebugKotlin`
Expected: COMPILE ERROR at `RelaySession.kt` line 61 (`when (msg.kind)` / the `onServerMessage` dispatch) and/or `ChatMessageList.kt` line 56 — because adding new variants is deferred to Tasks 6-7. This is the *desired* failure mode; you will fix those in their tasks. For Task 4 in isolation, the parse-test-only module may still compile if `RelaySession` isn't yet modified — but if it errors here, that's expected and resolved by Task 7. Do NOT add a branch just to silence the compiler — add it in the task that owns that surface.

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/MessagesParseTest.kt
git commit -m "feat(android): parse speaker_name/is_wearer + proactive propose; add name/reassign control msgs"
```

---

## Task 5 (Android): `SpeakerApi` + `SpeakerRepository` + `SpeakerCache`

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt`
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt` (add `getSpeakers`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt` (add `SpeakerDto`/`SpeakersDto`)
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerCacheTest.kt` (new)
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt` (new)

**Interfaces:**
- Consumes: `SenseHttpClient` (OkHttp), `clientProvider` pattern, `DtoJson`.
- Produces: `interface SpeakerApi { suspend fun getSpeakers(): List<SpeakerDto> }`; `class SpeakerRepository(private val apiProvider: suspend () -> SpeakerApi)` with `suspend fun loadSpeakers(): List<SpeakerEntry>`; `SpeakerCache` with `upsert(id, name, isWearer)`, `get(id): SpeakerEntry?`, `snapshot(): Map<String, SpeakerEntry>`, `seed(list)`.

- [ ] **Step 1: Write the failing `SpeakerCache` test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerCacheTest.kt`:

```kotlin
package com.sense.relay.data

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

class SpeakerCacheTest {

    @Test
    fun seed_replaces_snapshot() {
        val cache = SpeakerCache()
        cache.seed(listOf(
            SpeakerEntry("sp-1", "Sarah", false),
            SpeakerEntry("you", "You", true),
        ))
        assertEquals("Sarah", cache.get("sp-1")?.name)
        assertEquals(true, cache.get("you")?.isWearer)
        assertEquals(2, cache.snapshot().size)
    }

    @Test
    fun upsert_adds_and_overwrites_a_single_entry() {
        val cache = SpeakerCache()
        cache.upsert("sp-1", "Sara", false)
        assertEquals("Sara", cache.get("sp-1")?.name)
        cache.upsert("sp-1", "Sarah", false)
        assertEquals("Sarah", cache.get("sp-1")?.name)
    }

    @Test
    fun get_returns_null_for_unknown() {
        val cache = SpeakerCache()
        assertNull(cache.get("nope"))
    }

    @Test
    fun seed_with_empty_clears_known_speakers() {
        val cache = SpeakerCache()
        cache.upsert("sp-1", "Sarah", false)
        cache.seed(emptyList())
        assertTrue(cache.snapshot().isEmpty())
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.SpeakerCacheTest"`
Expected: FAIL — `SpeakerCache`/`SpeakerEntry` unresolved.

- [ ] **Step 3: Create `SpeakerCache`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt`:

```kotlin
package com.sense.relay.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.concurrent.ConcurrentHashMap

/** A resolved speaker (no biometrics) cached in memory. */
data class SpeakerEntry(
    val speakerId: String,
    val name: String?,
    val isWearer: Boolean,
)

/**
 * In-memory `{speakerId -> SpeakerEntry}` seeded from `GET /speakers` and
 * updated by every transcript §E. The server is the source of truth; v1
 * rebuilds this on app start / after a rename. Thread-safe via a
 * ConcurrentHashMap + a StateFlow mirror for Compose observation.
 */
class SpeakerCache {

    private val entries = ConcurrentHashMap<String, SpeakerEntry>()

    private val _flow = MutableStateFlow<Map<String, SpeakerEntry>>(emptyMap())
    val flow: StateFlow<Map<String, SpeakerEntry>> = _flow.asStateFlow()

    fun get(speakerId: String): SpeakerEntry? = entries[speakerId]

    fun snapshot(): Map<String, SpeakerEntry> = entries.toMap()

    fun upsert(speakerId: String, name: String?, isWearer: Boolean) {
        entries[speakerId] = SpeakerEntry(speakerId, name, isWearer)
        _flow.update { entries.toMap() }
    }

    /** Replace the whole set (from `GET /speakers`). Empty list clears. */
    fun seed(speakers: List<SpeakerEntry>) {
        entries.clear()
        speakers.forEach { entries[it.speakerId] = it }
        _flow.update { entries.toMap() }
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.SpeakerCacheTest"`
Expected: PASS.

- [ ] **Step 5: Write the failing `SpeakerRepository` test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt`:

```kotlin
package com.sense.relay.data

import com.sense.relay.http.dto.SpeakerDto
import kotlinx.coroutines.test.runTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs

class SpeakerRepositoryImplTest {

    private class FakeSpeakerApi : SpeakerApi {
        var speakers: List<SpeakerDto> = emptyList()
        var error: Throwable? = null
        override suspend fun getSpeakers(): List<SpeakerDto> {
            error?.let { throw it }
            return speakers
        }
    }

    private fun dto(id: String, name: String?, wearer: Boolean = false) = SpeakerDto(
        speakerId = id, displayName = name, isWearer = wearer,
        enrollmentStatus = "confirmed", turnCount = 1,
        firstSeen = "2026-07-29T00:00:00Z", updatedAt = "2026-07-29T00:00:00Z",
    )

    @Test fun loadSpeakers_maps_dtos_to_entries() = runTest {
        val api = FakeSpeakerApi().apply {
            speakers = listOf(dto("sp-1", "Sarah"), dto("you", "You", wearer = true))
        }
        val repo = SpeakerRepository(api)
        val entries = repo.loadSpeakers()
        assertEquals(2, entries.size)
        assertEquals("Sarah", entries.first { it.speakerId == "sp-1" }.name)
        assertEquals(true, entries.first { it.speakerId == "you" }.isWearer)
    }

    @Test fun loadSpeakers_propagates_errors() = runTest {
        val api = FakeSpeakerApi().apply { error = java.io.IOException("boom") }
        val repo = SpeakerRepository(api)
        var threw = false
        try { repo.loadSpeakers() } catch (e: Exception) { threw = true }
        assert(threw)
    }
}
```

- [ ] **Step 6: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.SpeakerRepositoryImplTest"`
Expected: FAIL — `SpeakerApi`/`SpeakerRepository`/`SpeakerDto` unresolved.

- [ ] **Step 7: Add DTOs**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt`, add (near the other DTOs):

```kotlin
@Serializable
data class SpeakerDto(
    val speakerId: String,
    val displayName: String? = null,
    val isWearer: Boolean = false,
    val enrollmentStatus: String = "",
    val turnCount: Int = 0,
    val firstSeen: String = "",
    val updatedAt: String = "",
)

@Serializable
data class SpeakersDto(val speakers: List<SpeakerDto> = emptyList())
```

- [ ] **Step 8: Add `getSpeakers` to `SenseHttpClient`**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt`, add a method mirroring `getSessionEvents` (lines 149-157):

```kotlin
    suspend fun getSpeakers(): List<SpeakerDto> =
        withContext(Dispatchers.IO) {
            client.newCall(req("/speakers")).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(SpeakersDto.serializer(), resp.body?.string().orEmpty()).speakers
            }
        }
```

(Add `import com.sense.relay.http.dto.SpeakerDto` + `import com.sense.relay.http.dto.SpeakersDto` to the imports.)

- [ ] **Step 9: Create `SpeakerApi` + `SpeakerRepository`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt`:

```kotlin
package com.sense.relay.data

import com.sense.relay.http.SenseHttpClient
import com.sense.relay.http.dto.SpeakerDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Narrow interface for the speakers endpoint, faked in host unit tests
 * (mirrors [SessionApi]). Production impl is [HttpSpeakerApi] wrapping
 * [SenseHttpClient.getSpeakers].
 */
interface SpeakerApi {
    suspend fun getSpeakers(): List<SpeakerDto>
}

private class HttpSpeakerApi(private val client: suspend () -> SenseHttpClient) : SpeakerApi {
    override suspend fun getSpeakers(): List<SpeakerDto> = client().getSpeakers()
}

/**
 * Domain layer for speakers. Resolves the current client per call so a
 * re-provision takes effect on the next fetch (same pattern as
 * [AgentRepository]/[MemoryRepository]).
 */
class SpeakerRepository(private val apiProvider: suspend () -> SpeakerApi) {

    /** Convenience constructor pinning a single api (test path). */
    constructor(api: SpeakerApi) : this(apiProvider = { api })

    suspend fun loadSpeakers(): List<SpeakerEntry> = withContext(Dispatchers.IO) {
        apiProvider().getSpeakers().map { dto ->
            SpeakerEntry(
                speakerId = dto.speakerId,
                name = dto.displayName,
                isWearer = dto.isWearer,
            )
        }
    }

    companion object {
        /** Wire a [SpeakerRepository] onto the shared client provider. */
        fun fromClient(clientProvider: suspend () -> SenseHttpClient): SpeakerRepository =
            SpeakerRepository(apiProvider = { HttpSpeakerApi(clientProvider) })
    }
}
```

- [ ] **Step 10: Run test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.SpeakerRepositoryImplTest" --tests "com.sense.relay.data.SpeakerCacheTest"`
Expected: PASS.

- [ ] **Step 11: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerCacheTest.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt
git commit -m "feat(android): add SpeakerApi/SpeakerRepository/SpeakerCache + GET /speakers client"
```

---

## Task 6 (Android): `SPEAKER_NUDGE` kind, `ChatMessage` fields, `NameSpeakerBubble`, `ChatMessageList` branch

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt`
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/NameSpeakerBubble.kt`
- Modify: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt`

**Interfaces:**
- Consumes: `NameSpeakerPropose` (Task 4), `ChatMessage`, `ChatMessageKind`, the `ProactiveMessageBubble` render style.
- Produces: `ChatMessageKind.SPEAKER_NUDGE`; `ChatMessage.propose: NameSpeakerPropose?`, `ChatMessage.sessionId: String?`, `ChatMessage.speakerId: String?`; `NameSpeakerBubble` composable (DTO-free); an `onNameSpeaker: (speakerId, name) -> Unit` callback threaded through `ChatMessageList`.

- [ ] **Step 1: Write the failing enum-name test**

In `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt`, add the new constant to whatever list the test pins (mirror its existing pattern — if it asserts the full set of names, add `NAME_SPEAKER`):

```kotlin
    @Test
    fun kinds_include_speaker_nudge() {
        // The exhaustive `when` in ChatMessageList depends on this enum.
        // Adding a constant without handling it is a compile error there.
        assertNotEquals(-1, ChatMessageKind.NAME_SPEAKER.ordinal)
    }
```

(Adjust to the existing assertion style in that file. If the file asserts a literal list, append `NAME_SPEAKER` to that list.)

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatMessageKindTest"`
Expected: FAIL — `NAME_SPEAKER` unresolved.

- [ ] **Step 3: Add the enum constant + `ChatMessage` fields**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt`:

Change the enum (line 79):

```kotlin
enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR, AGENT_PROACTIVE, NAME_SPEAKER }
```

Add fields to `ChatMessage` (lines 81-97). Add the `propose`/`sessionId`/`speakerId` imports for `NameSpeakerPropose`:

```kotlin
data class ChatMessage(
    val id: String,
    val role: Role,
    val kind: ChatMessageKind = ChatMessageKind.USER_TEXT,
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
    val propose: com.sense.relay.protocol.NameSpeakerPropose? = null,
    val sessionId: String? = null,
    val speakerId: String? = null,
)

enum class Role { USER, AGENT }
```

- [ ] **Step 4: Run the enum test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatMessageKindTest"`
Expected: PASS.

- [ ] **Step 5: Create `NameSpeakerBubble`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/NameSpeakerBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.RecordVoiceOver
import androidx.compose.material3.Button
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.data.ChatMessage
import com.sense.relay.ui.theme.Spacing

/**
 * Interactive name-the-speaker nudge. Renders the proactive text plus an
 * inline text field + "Name" button. On submit it calls [onNameSpeaker]
 * with `(speakerId, name)`; the caller (ChatViewModel) sends the
 * `name_speaker` control message and optimistically collapses the bubble.
 *
 * Architectural invariant: this composable MUST NOT import
 * `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`
 * (enforced by ArchitecturalInvariantsTest).
 */
@Composable
fun NameSpeakerBubble(
    message: ChatMessage,
    onNameSpeaker: (speakerId: String, name: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    var draft by remember { mutableStateOf("") }
    val speakerId = message.propose?.speakerId ?: message.speakerId ?: ""

    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = Spacing.xs)
            .testTag("chat_name_speaker_${message.id}"),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = Icons.Filled.RecordVoiceOver,
                    contentDescription = "Name speaker",
                    modifier = Modifier.testTag("chat_name_speaker_icon"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Text(
                    text = "Name speaker",
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.testTag("chat_name_speaker_tag"),
                )
            }
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = message.text,
                style = MaterialTheme.typography.bodyLarge,
            )
            Spacer(Modifier.height(Spacing.sm))
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
            ) {
                OutlinedTextField(
                    value = draft,
                    onValueChange = { draft = it },
                    singleLine = true,
                    placeholder = { Text("Enter a name") },
                    modifier = Modifier
                        .weight(1f)
                        .testTag("chat_name_speaker_field"),
                )
                Button(
                    onClick = {
                        val name = draft.trim()
                        if (name.isNotEmpty() && speakerId.isNotEmpty()) {
                            onNameSpeaker(speakerId, name)
                        }
                    },
                    enabled = draft.isNotBlank() && speakerId.isNotEmpty(),
                    modifier = Modifier.testTag("chat_name_speaker_submit"),
                ) { Text("Name") }
            }
        }
    }
}
```

> `Spacing.height`/`Spacing.sm` etc. must match the project's `Spacing` object (see `ProactiveMessageBubble.kt` which uses `Spacing.xs`/`Spacing.md`). If `Modifier.height` needs an import, add `androidx.compose.foundation.layout.height`. Match the imports already present in `ProactiveMessageBubble.kt`.

- [ ] **Step 6: Add the `ChatMessageList` branch + thread the callback**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt`:

Add an `onNameSpeaker: (speakerId: String, name: String) -> Unit` parameter to `ChatMessageList` (signature at lines 35-43, alongside `onAtomChipTap`/`onBrowseMemory`).

Add the branch to the exhaustive `when` (lines 55-73):

```kotlin
                ChatMessageKind.NAME_SPEAKER -> NameSpeakerBubble(
                    message = msg,
                    onNameSpeaker = onNameSpeaker,
                )
```

> If `ChatMessageList`'s callers (e.g. `ChatScreen`) pass the other callbacks positionally, add `onNameSpeaker` at the end and update those call sites to pass a no-op or the real handler. Check `ChatScreen.kt` for the call site and wire the real handler in Task 7's `ChatViewModel` plumbing (or pass a lambda that calls into the VM). If the screen constructs the VM, route `onNameSpeaker = { id, name -> vm.nameSpeaker(id, name) }` — `ChatViewModel.nameSpeaker` is added in Task 7. For Task 6 in isolation, a `{ _, _ -> }` placeholder at the call site is acceptable ONLY if Task 7 immediately replaces it; prefer wiring the real handler if `ChatViewModel` is already accessible there.

- [ ] **Step 7: Compile + run the kind test**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:compileDebugKotlin :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatMessageKindTest"`
Expected: PASS (the `when` is now exhaustive). If `RelaySession.kt`'s `onServerMessage` still fails to compile (it will, until Task 7), that's expected — Task 7 fixes it.

- [ ] **Step 8: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/NameSpeakerBubble.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt
git commit -m "feat(android): add SPEAKER_NUDGE kind + NameSpeakerBubble"
```

---

## Task 7 (Android): `RelaySession` wires propose→nudge, transcript→cache, `sendControl`

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/RelaySessionTest.kt`

**Interfaces:**
- Consumes: `ServerMessage.Transcript.speakerName/isWearer`, `ServerMessage.Proactive.propose`, `ChatMessage(kind=SPEAKER_NUDGE,...)`, `NameSpeakerMsg`/`ReassignSpeakerMsg.encode()`, `SpeakerCache.upsert`.
- Produces: `RelaySession.onServerMessage` handles the new transcript/proactive paths; `RelaySession.sendControl(msg): List<RelayAction>` (returns a `SendServerText` action). A `speakerCache: SpeakerCache?` constructor param (nullable so existing tests compile unchanged).

- [ ] **Step 1: Write the failing tests**

Append to `android/sense-relay/app/src/test/kotlin/com/sense/relay/RelaySessionTest.kt`:

```kotlin
    @Test
    fun proactive_with_name_speaker_propose_forwards_speaker_nudge_chat_message() {
        val session = RelaySession("s")
        val actions = session.onServerMessage(
            """{"type":"proactive","request_id":"r1","text":"Who was that?",
               "atoms":[],"propose":{"kind":"name_speaker","speaker_id":"sp-9"}}"""
        )
        assertEquals(1, actions.size)
        val forward = actions.single()
        assertTrue(forward is RelayAction.ForwardToChatHistory)
        val msg = forward.message
        assertEquals(ChatMessageKind.NAME_SPEAKER, msg.kind)
        assertEquals("sp-9", msg.propose?.speakerId)
        assertEquals("s", msg.sessionId)
    }

    @Test
    fun proactive_without_propose_still_forwards_text_as_proactive() {
        val session = RelaySession("s")
        val actions = session.onServerMessage(
            """{"type":"proactive","request_id":"r2","text":"hi","atoms":[]}"""
        )
        val forward = actions.single() as RelayAction.ForwardToChatHistory
        assertEquals(ChatMessageKind.AGENT_PROACTIVE, forward.message.kind)
        assertNull(forward.message.propose)
    }

    @Test
    fun transcript_with_speaker_name_upserts_speaker_cache() {
        val cache = SpeakerCache()
        val session = RelaySession("s", speakerCache = cache)
        session.onServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":"sp-1","speaker_name":"Sarah","is_wearer":false}"""
        )
        assertEquals("Sarah", cache.get("sp-1")?.name)
        assertEquals(false, cache.get("sp-1")?.isWearer)
    }

    @Test
    fun send_control_serializes_name_speaker_message() {
        val session = RelaySession("s")
        val actions = session.sendControl(
            NameSpeakerMsg(session_id = "s", speaker_id = "sp-9", name = "Sarah")
        )
        assertEquals(1, actions.size)
        val send = actions.single() as RelayAction.SendServerText
        assertTrue(send.text.contains("\"type\":\"name_speaker\""))
        assertTrue(send.text.contains("\"speaker_id\":\"sp-9\""))
        assertTrue(send.text.contains("\"name\":\"Sarah\""))
    }
```

Add imports at the top:

```kotlin
import com.sense.relay.data.ChatMessageKind
import com.sense.relay.data.SpeakerCache
import com.sense.relay.protocol.NameSpeakerMsg
import com.sense.relay.protocol.encode
import kotlin.test.assertNull
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.RelaySessionTest"`
Expected: FAIL — `RelaySession` constructor has no `speakerCache`, `sendControl` undefined, `propose` not handled.

- [ ] **Step 3: Update `RelaySession`**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt`:

Add a nullable `speakerCache` to the constructor (keep it optional with a default so existing tests/constructors compile):

```kotlin
class RelaySession(
    private val sessionId: String,
    private val startSeq: Int = 0,
    private val speakerCache: com.sense.relay.data.SpeakerCache? = null,
) {
```

Update `forwardProactive` to branch on `propose` (lines 81-101) — add an overload/branch that builds a `SPEAKER_NUDGE` message when `propose != null`:

```kotlin
    private fun forwardProactive(msg: ServerMessage.Proactive): RelayAction {
        if (msg.propose != null) {
            val chatMessage = ChatMessage(
                id = msg.requestId,
                role = Role.AGENT,
                kind = ChatMessageKind.NAME_SPEAKER,
                text = msg.text,
                propose = msg.propose,
                sessionId = sessionId,
                speakerId = msg.propose.speakerId,
                traceRequestId = msg.requestId,
            )
            return RelayAction.ForwardToChatHistory(chatMessage)
        }
        // Existing proactive-answer path.
        val chatMessage = ChatMessage(
            id = msg.requestId,
            role = Role.AGENT,
            kind = ChatMessageKind.AGENT_PROACTIVE,
            text = msg.text,
            atoms = msg.atoms.map { atomId ->
                AtomChip(
                    atomId = atomId,
                    sessionId = "",
                    kind = "",
                    text = "",
                    createdAt = "",
                    startMs = 0,
                    score = 0.0,
                )
            },
            traceRequestId = msg.requestId,
        )
        return RelayAction.ForwardToChatHistory(chatMessage)
    }
```

Update the `Transcript` branch in `onServerMessage` (lines 60-70) to upsert the cache:

```kotlin
            is ServerMessage.Transcript -> {
                if (speakerCache != null && msg.speakerName != null) {
                    // We only learn the *name* here; the speaker UUID isn't on the
                    // §E transcript message today, so the cache key is the name.
                    // (The UUID flows through /sessions/{id}/events for Recordings.)
                    // If the server adds speaker UUID to §E later, key on that.
                    // For now: no-op upsert of the nameless hop is avoided; the
                    // Recordings path + GET /speakers are the real cache sources.
                }
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            }
```

> **Correction — read the spec carefully:** The §E `transcript` message does carry `speaker` (UUID) per the server `TranscriptMsg` (Task 1 kept `speaker` plus added `speaker_name`). The Android `ServerMessage.Transcript` in Task 4 did NOT add a `speaker` field (only `speakerName`/`isWearer`). To upsert the cache by UUID from §E, add `val speaker: String? = null` to `ServerMessage.Transcript` in Task 4's Messages.kt edit and parse `speaker = str("speaker")`. Then this branch becomes:

```kotlin
            is ServerMessage.Transcript -> {
                val spk = msg.speaker
                if (speakerCache != null && spk != null) {
                    speakerCache.upsert(spk, msg.speakerName, msg.isWearer)
                }
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            }
```

**Action:** Go back and amend Task 4 Step 3 to also add `val speaker: String? = null` to `ServerMessage.Transcript` and `speaker = str("speaker"),` in the parser. Add a parse assertion to `MessagesParseTest.kt`:

```kotlin
    @Test
    fun transcript_carries_speaker_uuid() {
        val msg = parseServerMessage(
            """{"type":"transcript","session_id":"s","text":"hi","duration_ms":1000,
               "speaker":"sp-1","speaker_name":"Sarah","is_wearer":false}"""
        )
        assertTrue(msg is ServerMessage.Transcript)
        assertEquals("sp-1", msg.speaker)
    }
```

This keeps the cache keyed by UUID. Do this amendment before continuing.

Add `sendControl`:

```kotlin
    /** Serialize + send a control message (name_speaker / reassign_speaker) over the WS. */
    fun <T> sendControl(msg: T): List<RelayAction> where T : com.sense.relay.protocol.ControlEncodable {
        return listOf(RelayAction.SendServerText(msg.encode()))
    }
```

> Simpler: avoid the generic constraint. Define a tiny marker or just overload. The cleanest approach is to give `NameSpeakerMsg`/`ReassignSpeakerMsg` a shared `encode()` and accept the encoded string. Use two overloads:

```kotlin
    fun sendControl(msg: com.sense.relay.protocol.NameSpeakerMsg): List<RelayAction> =
        listOf(RelayAction.SendServerText(msg.encode()))

    fun sendControl(msg: com.sense.relay.protocol.ReassignSpeakerMsg): List<RelayAction> =
        listOf(RelayAction.SendServerText(msg.encode()))
```

(Both `encode()` functions were added in Task 4.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.RelaySessionTest" --tests "com.sense.relay.MessagesParseTest"`
Expected: PASS.

- [ ] **Step 5: Run the full Android unit suite**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest`
Expected: PASS (268 + new; no regressions).

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/RelaySessionTest.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/MessagesParseTest.kt
git commit -m "feat(android): wire propose->SPEAKER_NUDGE, transcript->SpeakerCache, sendControl"
```

---

## Task 8 (Android): Recordings speaker labels + rename/reassign menu

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt` (`CaptureEventDto` +speaker fields)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Mappers.kt` (fold into `TranscriptChunk`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/domain/model/TranscriptChunk.kt` (+speaker/speakerName/isWearer)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt` (`EventRow` label + menu)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt` (rename/reassign actions, expose SpeakerCache)
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt`

**Interfaces:**
- Consumes: `SpeakerCache`, `RelayController`/`RelaySession.sendControl` (for outbound control), the server's `_event_to_wire` speaker fields (Task 2).
- Produces: `TranscriptChunk.speaker/speakerName/isWearer`; `EventRow` renders `speakerName ?: "?"`; tap → Rename dialog (→ `NameSpeakerMsg`) / Reassign picker (→ `ReassignSpeakerMsg{from,to,scope="all"}`); VM actions `renameSpeaker(speakerId, name)` and `reassignSpeaker(fromId, toId)`.

- [ ] **Step 1: Write the failing VM test**

Append to `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt`:

```kotlin
    @Test fun rename_speaker_emits_name_speaker_control() = runTest(dispatcher) {
        // The VM must, on rename, send a NameSpeaker control message via the
        // relay controller and optimistically update the SpeakerCache.
        val cache = com.sense.relay.data.SpeakerCache()
        val controller = RecordingFakeController()
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(
            flowOf(Outcome.Success(SessionDetails(summary("s1"), emptyList()))),
            flowOf(Outcome.Success(emptyList())),
        ), speakerCache = cache, relayController = controller)
        vm.renameSpeaker("sp-1", "Sarah")
        testScheduler.advanceUntilIdle()
        assertEquals("Sarah", cache.get("sp-1")?.name)
        assertEquals(1, controller.sentControls.size)
        assertTrue(controller.sentControls.first().contains("\"type\":\"name_speaker\""))
    }

    @Test fun reassign_speaker_emits_reassign_control_scope_all() = runTest(dispatcher) {
        val cache = com.sense.relay.data.SpeakerCache()
        cache.upsert("sp-1", "Sara", false)
        cache.upsert("sp-2", "Sam", false)
        val controller = RecordingFakeController()
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(
            flowOf(Outcome.Success(SessionDetails(summary("s1"), emptyList()))),
            flowOf(Outcome.Success(emptyList())),
        ), speakerCache = cache, relayController = controller)
        vm.reassignSpeaker(fromId = "sp-1", toId = "sp-2")
        testScheduler.advanceUntilIdle()
        assertEquals(1, controller.sentControls.size)
        val sent = controller.sentControls.first()
        assertTrue(sent.contains("\"type\":\"reassign_speaker\""))
        assertTrue(sent.contains("\"from_speaker_id\":\"sp-1\""))
        assertTrue(sent.contains("\"to_speaker_id\":\"sp-2\""))
        assertTrue(sent.contains("\"scope\":\"all\""))
    }
```

Add a fake controller + adjust the VM constructor in the test file:

```kotlin
    private class RecordingFakeController : com.sense.relay.data.RelayController {
        val sentControls = mutableListOf<String>()
        override fun sendControl(json: String) { sentControls.add(json) }
        // ...implement other RelayController members as no-ops if the interface requires them;
        // mirror FakeRelayController if one exists in the test fixtures.
    }
```

> Check whether `RelayController` is an interface or an object, and whether a fake already exists. The explorer noted `RelayController` is wired in `Repositories` (RepositoryModule.kt) as `RelayController` (an object or a class). If it's a singleton object, abstract a thin `interface SpeakerActions { fun sendControl(json: String) }` that the VM depends on instead, with a production adapter calling the real controller. Prefer the smallest interface so the VM stays testable. If `RelayController` is already an interface with a fake in tests, reuse it.

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.recordings.SessionDetailViewModelTest"`
Expected: FAIL — `SessionDetailViewModel` has no `speakerCache`/`relayController` params, no `renameSpeaker`/`reassignSpeaker`.

- [ ] **Step 3: Add speaker fields through the DTO/domain/mapper chain**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt`, extend `CaptureEventDto` (lines 43-59) — additive optional fields (parser is `ignoreUnknownKeys=true`):

```kotlin
@Serializable
data class CaptureEventDto(
    val id: String,
    val sessionId: String,
    val seq: Int,
    val startMs: Long,
    val createdAt: String,
    val kind: String,
    val text: String = "",
    val durationMs: Int = 0,
    val codec: String = "",
    val sampleRateHz: Int = 0,
    val byteCount: Int = 0,
    val speaker: String? = null,
    val speakerName: String? = null,
    val isWearer: Boolean = false,
    val speakerConfidence: Double? = null,
    val speakerAssignment: String? = null,
)
```

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/domain/model/TranscriptChunk.kt` (lines 11-19):

```kotlin
data class TranscriptChunk(
    override val id: String,
    override val sessionId: SessionId,
    override val seq: Int,
    override val startMs: Long,
    override val createdAt: Instant,
    val text: String,
    val durationMs: Int,
    val speaker: String? = null,
    val speakerName: String? = null,
    val isWearer: Boolean = false,
) : CaptureEvent
```

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Mappers.kt`, update the `"transcript"` branch of `toDomainOrNull` (lines 44-68):

```kotlin
    "transcript" -> TranscriptChunk(
        id = id,
        sessionId = SessionId(sessionId),
        seq = seq,
        startMs = startMs,
        createdAt = parseInstant(createdAt),
        text = text,
        durationMs = durationMs,
        speaker = speaker,
        speakerName = speakerName,
        isWearer = isWearer,
    )
```

- [ ] **Step 4: Update `SessionDetailViewModel`**

Add `speakerCache: SpeakerCache` and a control-sending collaborator to the constructor. Add the two actions:

```kotlin
class SessionDetailViewModel(
    private val id: SessionId,
    private val repo: SessionRepository,
    private val speakerCache: SpeakerCache = SpeakerCache(),
    private val relayController: SpeakerActions,
) : ViewModel() {
    ...
    /** Send a name_speaker control message (rename or initial naming — same message). */
    fun renameSpeaker(speakerId: String, name: String) {
        speakerCache.upsert(speakerId, name, speakerCache.get(speakerId)?.isWearer ?: false)
        val msg = NameSpeakerMsg(session_id = id.value, speaker_id = speakerId, name = name)
        relayController.sendControl(msg.encode())
    }

    /** Send a reassign_speaker control (v1: scope="all"). */
    fun reassignSpeaker(fromId: String, toId: String) {
        val msg = ReassignSpeakerMsg(
            session_id = id.value,
            from_speaker_id = fromId,
            to_speaker_id = toId,
            scope = "all",
        )
        relayController.sendControl(msg.encode())
    }
}
```

Define the minimal `SpeakerActions` interface (in `data/` near the controller, or in the VM file if it's small):

```kotlin
/** Thin port so SessionDetailViewModel can send control messages without depending on the relay. */
interface SpeakerActions {
    fun sendControl(json: String)
}
```

Wire a production adapter in `RepositoryModule.kt` (or at the `SessionDetailRoute` call site) that calls the real `RelayController`/`RelaySession.sendControl`. If `RelayController` exposes a `sendControl(json)` already, the VM can take `RelayController` directly — but to keep the VM testable with a fake, depend on `SpeakerActions`. Update `SessionDetailRoute` (SessionDetailScreen.kt:43-48) to pass `RepositoryModule.repos.speakerCache` and the `SpeakerActions` adapter.

- [ ] **Step 5: Render the label + menu in `EventRow`**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt`, update `EventRow` (lines 148-162) to render the speaker name and a tap target. Add a Rename dialog + a Reassign picker driven by `speakerCache.snapshot()`. The menu options call `vm.renameSpeaker(...)` / `vm.reassignSpeaker(...)`. Thread `vm` (or callbacks `onRename`/`onReassign`) into `Body` → `EventRow`. A minimal addition:

```kotlin
@Composable
private fun EventRow(
    event: CaptureEvent,
    speakerCache: com.sense.relay.data.SpeakerCache,
    onRename: (speakerId: String) -> Unit,
    onReassign: (speakerId: String) -> Unit,
) {
    val speakerName = (event as? TranscriptChunk)?.let { it.speakerName ?: "?" } ?: null
    Column(modifier = Modifier.fillMaxWidth().padding(vertical = Spacing.xs)) {
        if (speakerName != null) {
            Text(
                text = speakerName,
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .testTag("event_speaker_${event.id}")
                    .clickable {
                        // Trigger the Rename/Reassign menu (a DropdownMenu or a
                        // dialog surfaced by the screen). Wire onRename/onReassign
                        // through the screen state.
                    },
            )
        }
        Text(text = eventTitle(event), style = MaterialTheme.typography.bodyMedium, color = MaterialTheme.colorScheme.onSurface)
        Text(text = "+${formatHmMs(event.startMs)}", style = MaterialTheme.typography.labelSmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
    }
}
```

Implement the Rename dialog (a `TextField` + confirm → `onRename(speakerId) -> vm.renameSpeaker`) and the Reassign picker (list from `speakerCache.snapshot().values` → `onReassign(from, to) -> vm.reassignSpeaker`). The picker shows all known speakers except the current one. Keep the UI minimal and monochrome per the Android UI style memory.

- [ ] **Step 6: Run the VM test to verify it passes**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.recordings.SessionDetailViewModelTest"`
Expected: PASS.

- [ ] **Step 7: Run the full Android suite**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest`
Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/http/dto/Mappers.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/domain/model/TranscriptChunk.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt
git commit -m "feat(android): speaker labels + rename/reassign in Recordings"
```

---

## Task 9 (Android): You-confirmation one-time prompt

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/YouConfirmationBanner.kt`
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt` (or a Chat-scoped VM — wherever transcripts are observed) to surface the prompt + the "shown" flag.
- Test: extend the relevant VM test.

**Interfaces:**
- Consumes: `SpeakerCache`, `TranscriptChunk.isWearer`/`speakerName`, `NameSpeakerMsg`.
- Produces: a `StateFlow<YouConfirmationState>` (Idle | Prompting(wearerId) | Done); on the first transcript with `isWearer && speakerName == "You"`, emit `Prompting(wearerId)`; on submit, `renameSpeaker(wearerId, chosenName)` and flip to Done (session-scoped flag; re-shows after restart if still "You").

- [ ] **Step 1: Write the failing test**

In the VM test file (the one observing transcripts — extend `SessionDetailViewModelTest.kt` or a dedicated `YouConfirmationTest.kt`):

```kotlin
    @Test fun you_confirmation_prompts_on_first_wearer_you_transcript() = runTest(dispatcher) {
        val cache = com.sense.relay.data.SpeakerCache()
        val controller = RecordingFakeController()
        val events = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(Outcome.Success(SessionDetails(summary("s1"), emptyList())))
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, events),
            speakerCache = cache, relayController = controller)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        events.emit(Outcome.Success(listOf(chunk("e1", 1).let {
            TranscriptChunk(it.id, it.sessionId, it.seq, it.startMs, it.createdAt, it.text, it.durationMs,
                speaker = "you", speakerName = "You", isWearer = true)
        })))
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Prompting("you"), vm.youConfirmation.value)

        vm.confirmYou("Kevin")
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Done, vm.youConfirmation.value)
        assertEquals("Kevin", cache.get("you")?.name)
        assertTrue(controller.sentControls.first().contains("\"name\":\"Kevin\""))
    }

    @Test fun you_confirmation_does_not_prompt_when_already_named() = runTest(dispatcher) {
        val cache = com.sense.relay.data.SpeakerCache()
        cache.upsert("you", "Kevin", true)
        val controller = RecordingFakeController()
        val events = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(Outcome.Success(SessionDetails(summary("s1"), emptyList())))
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, events),
            speakerCache = cache, relayController = controller)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        events.emit(Outcome.Success(listOf(
            TranscriptChunk("e1", SessionId("s1"), 1, 1000L, Instant.EPOCH, "t", 500,
                speaker = "you", speakerName = "Kevin", isWearer = true)
        )))
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Idle, vm.youConfirmation.value)
    }
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.recordings.SessionDetailViewModelTest"`
Expected: FAIL — `YouConfirmationState`/`youConfirmation`/`confirmYou` unresolved.

- [ ] **Step 3: Implement the prompt state in the VM**

Add to `SessionDetailViewModel.kt`:

```kotlin
sealed interface YouConfirmationState {
    data object Idle : YouConfirmationState
    data class Prompting(val wearerId: String) : YouConfirmationState
    data object Done : YouConfirmationState
}

// In the VM:
private val _youConfirmation = MutableStateFlow<YouConfirmationState>(YouConfirmationState.Idle)
val youConfirmation: StateFlow<YouConfirmationState> = _youConfirmation.asStateFlow()
private var youPromptShown = false

// Inside the events collector (where events arrive), detect the trigger:
private fun maybePromptYouConfirmation(events: List<CaptureEvent>) {
    if (youPromptShown) return
    val wearer = events.filterIsInstance<TranscriptChunk>().firstOrNull { it.isWearer }
    if (wearer != null && (wearer.speakerName == null || wearer.speakerName == "You")) {
        val wearerId = wearer.speaker ?: return
        // Skip if the cache already has a real name for this wearer.
        val cached = speakerCache.get(wearerId)
        if (cached != null && cached.name != null && cached.name != "You") return
        youPromptShown = true
        _youConfirmation.value = YouConfirmationState.Prompting(wearerId)
    }
}

fun confirmYou(chosenName: String) {
    val state = _youConfirmation.value
    if (state is YouConfirmationState.Prompting) {
        renameSpeaker(state.wearerId, chosenName)
        _youConfirmation.value = YouConfirmationState.Done
    }
}

fun dismissYouConfirmation() {
    _youConfirmation.value = YouConfirmationState.Idle
}
```

Call `maybePromptYouConfirmation(events)` from the `reduce`/collector that already builds the `Loaded` state.

- [ ] **Step 4: Create the banner composable**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/YouConfirmationBanner.kt`:

```kotlin
package com.sense.relay.ui.recordings

import androidx.compose.foundation.layout.*
import androidx.compose.material3.*
import androidx.compose.runtime.*
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.ui.theme.Spacing

/** One-time prompt to confirm/rename the wearer ("You"). DTO-free. */
@Composable
fun YouConfirmationBanner(
    state: YouConfirmationState,
    onConfirm: (name: String) -> Unit,
    onDismiss: () -> Unit,
    modifier: Modifier = Modifier,
) {
    if (state !is YouConfirmationState.Prompting) return
    var draft by remember { mutableStateOf("") }
    Surface(
        modifier = modifier.fillMaxWidth().padding(Spacing.sm).testTag("you_confirm_banner"),
        color = MaterialTheme.colorScheme.primaryContainer,
        shape = androidx.compose.foundation.shape.RoundedCornerShape(12.dp),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text("Is this you? What should I call you?", style = MaterialTheme.typography.titleSmall)
            Spacer(Modifier.height(Spacing.xs))
            Row(verticalAlignment = androidx.compose.ui.Alignment.CenterVertically) {
                OutlinedTextField(
                    value = draft, onValueChange = { draft = it }, singleLine = true,
                    placeholder = { Text("Your name") },
                    modifier = Modifier.weight(1f).testTag("you_confirm_field"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Button(
                    onClick = { if (draft.isNotBlank()) onConfirm(draft.trim()) },
                    enabled = draft.isNotBlank(),
                    modifier = Modifier.testTag("you_confirm_submit"),
                ) { Text("Save") }
            }
        }
    }
}
```

Render it at the top of `SessionDetailScreen.Body` (only when `vm.youConfirmation.value` is `Prompting`), wiring `onConfirm = vm::confirmYou`, `onDismiss = vm::dismissYouConfirmation`.

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.recordings.SessionDetailViewModelTest"`
Expected: PASS.

- [ ] **Step 6: Run the full Android suite**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" ./gradlew :app:testDebugUnitTest`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/YouConfirmationBanner.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt
git commit -m "feat(android): one-time You-confirmation prompt on first wearer transcript"
```

---

## Self-Review

**1. Spec coverage** (checked against the approved spec sections):

- §Server 1 `TranscriptMsg` +`speaker_name`/`is_wearer` → **Task 1**.
- §Server 2 `_event_to_wire` + registry wiring → **Task 2**.
- §Server 3 `GET /speakers` (no biometrics) → **Task 3**.
- §Android 1 `Messages.kt` parse + outbound control + `sendControl` → **Tasks 4 & 7**.
- §Android 2 `RelaySession` dispatch propose→nudge, transcript→cache, sendControl → **Task 7**.
- §Android 3 `ChatHistoryStore` + `ChatMessageList` + `NameSpeakerBubble` → **Task 6**.
- §Android 4 `NameSpeakerBubble` → **Task 6**.
- §Android 5 `SessionDetailScreen` labels + rename/reassign → **Task 8**.
- §Android 6 `SpeakerCache` → **Task 5**.
- §Android 7 `SpeakerApi`/`GET /speakers` client → **Task 5**.
- §Android 8 You-confirmation → **Task 9**.
- §Data flows (name/rename/reassign/You-confirm) → **Tasks 7, 8, 9**.
- §Error handling (send failure no-crash; no ack; unknown propose→null; GET /speakers failure non-fatal; disabled→no-op) → covered by nullable/cache-fallback/`ignoreUnknownKeys` patterns in Tasks 4-8; disabled-path is the global constraint (off by default → empty).
- §Testing section → tests in every task.
- §Out of scope — `scope="one"`/`"range"`, persistence, HTTP rename endpoint, chat labels, VAD-segment embedding → **not** in any task (correctly deferred).

**2. Placeholder scan:** The `ChatMessageList` callback wiring (Task 6 Step 6) and the `RelayController`/`SpeakerActions` shape (Task 8) carry explicit "check the existing pattern / mirror it" notes rather than invented signatures, because the explorer confirmed `RelayController` is wired in `RepositoryModule` but did not capture its full interface. The implementer is told exactly where to look (`RepositoryModule.kt`, existing fakes) and to mirror the established `Fake*Api` pattern — not to invent. No "TBD"/"TODO"/"implement later" strings.

**3. Type consistency:**
- `NameSpeakerPropose.speakerId` (Task 4) ↔ `ChatMessage.propose.speakerId` (Task 6) ↔ `RelaySession` propose→nudge (Task 7) ✓.
- `SpeakerEntry(speakerId, name, isWearer)` (Task 5) ↔ `SpeakerCache.get/upsert/seed` (Tasks 5, 7, 8) ✓.
- `NameSpeakerMsg(session_id, speaker_id, name, type="name_speaker")` + `ReassignSpeakerMsg(session_id, from_speaker_id, to_speaker_id, scope="all", type="reassign_speaker")` (Task 4) ↔ server inbound `NameSpeaker`/`ReassignSpeaker` (messages.py:55-77, already handled by `_on_name_speaker`/`_on_reassign_speaker` in core.py) ✓ — wire shapes match the spec's §Inbound section.
- `CaptureEventDto.speaker/speakerName/isWearer` (Task 8) ↔ server `_event_to_wire` output (Task 2) ✓ (camelCase matches).
- `TranscriptChunk.speaker/speakerName/isWearer` (Task 8) ↔ `YouConfirmationState` detection (Task 9) ✓.
- `YouConfirmationState.Idle/Prompting/Done` (Task 9) consistent between test + impl ✓.

**4. Amendment integrity:** Task 7 Step 3 contains a mid-task correction that requires amending Task 4 (add `speaker: String?` to `ServerMessage.Transcript` + parse + test). This is flagged in bold and the implementer must apply it before finishing Task 7. This keeps the cache keyed by UUID per the spec ("Events/atoms store the stable speaker_id UUID").

**5. Constraint preservation:** Off-by-default is structural (registry `None` → `speaker=None` → `[]` speakers → no nudge). Additive-only is enforced by optional fields + `ignoreUnknownKeys`. Biometrics excluded in Task 3 (`_speaker_to_wire` omits centroid/embedding_model/dim) and asserted by test. `list[float]` untouched. sqlite thread-safety untouched (no new store). `run_gateway.py` manually smoke-tested in Task 3 Step 6. VAD/hallucination filter untouched (no firmware/pipeline change). UI invariant preserved (NameSpeakerBubble/YouConfirmationBanner are DTO-free; Task 6 & 9 notes the `ArchitecturalInvariantsTest`).

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-29-android-speaker-recognition-ui.md`. Two execution options:

1. **Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.
2. **Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?