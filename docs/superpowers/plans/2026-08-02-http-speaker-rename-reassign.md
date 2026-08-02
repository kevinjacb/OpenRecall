# HTTP Speaker Rename/Reassign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move Android speaker rename/reassign from a WebSocket-only control-frame path (dead for offline/historical sessions) to HTTP endpoints that work for every session, and retire the now-unused WS send path on both ends.

**Architecture:** Two new server POST routes (`/speakers/{id}/rename`, `/speakers/reassign`) delegate to the already-implemented `SpeakerRegistry.name` / `reassign_speaker`. Android gains HTTP methods on `SenseHttpClient` → `SpeakerApi` → `SpeakerRepository` → a new `HttpSpeakerActions` that implements the existing `SpeakerActions` seam (now `suspend`). The two ViewModels call it on `viewModelScope`; rename is optimistic with revert-on-failure, reassign triggers an `onRefresh()` re-fetch. The WS outbound control-frame path (`SpeakerControlPort`, `RelayService` drain, `NameSpeaker`/`ReassignSpeaker` message types, server handlers) is removed last. The inbound `propose` nudge path is untouched.

**Tech Stack:** Server: Python 3, aiohttp, pydantic v2 (frozen DTOs), pytest. Android: Kotlin, kotlinx.serialization, OkHttp, Coroutines (`viewModelScope`), JUnit + MockWebServer-style fakes, Compose.

## Global Constraints

- Speaker recognition stays off by default (`SENSE_SPEAKER_ENABLED`); endpoints return 409 `speaker_recognition_disabled` when the registry is `None`.
- No biometrics over HTTP: `centroid`/`embedding_model`/`dim` never serialized — reuse the existing `_speaker_to_wire`.
- Reassign stays v1 whole-speaker (`scope` is `Literal["all"]`); per-row `one`/`range` is rejected with 400 (not silently accepted).
- Wire fields are camelCase to match the existing `GET /speakers` response (`speakerId`, `displayName`, …).
- Bearer-token auth is inherited from `bearer_auth_middleware`; handlers do not check `request["authorized"]` themselves (same as `/agent`).
- Additive-only, no schema migration; existing tests stay green; every sqlite store keeps `check_same_thread=False` + `Lock`.
- Each commit compiles and keeps its test suite green.

---

## File Structure

**Server (create/modify):**
- Modify `server/src/sense_server/http/routes/speakers.py` — add the two POST routes + two frozen DTOs + a `_bad_request` helper.
- Modify `server/tests/http/test_speakers.py` — add rename/reassign route tests; extend the `_client` fixture to accept `atom_store`.
- Modify `server/src/sense_server/gateway/core.py` — remove `_on_name_speaker`/`_on_reassign_speaker` + dispatch branches + imports.
- Modify `server/src/sense_server/protocol/messages.py` — remove `NameSpeaker`/`ReassignSpeaker`/`ReassignScope` + the `Inbound` union members.
- Modify `server/tests/gateway/test_core.py` — remove the two WS-handler tests.

**Android (create/modify):**
- Modify `app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt` — add 3 request/response DTOs.
- Modify `app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt` — add `renameSpeaker`/`reassignSpeaker` + JSON media type.
- Modify `app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt` — extend interface + `HttpSpeakerApi` + `SpeakerRepository`.
- Modify `app/src/main/kotlin/com/sense/relay/data/SpeakerActions.kt` — make methods `suspend`; add `HttpSpeakerActions`; remove `SpeakerControlPort`.
- Modify `app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt` — add `remove(id)`.
- Modify `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt` — suspend-call + revert/onRefresh + error flow.
- Modify `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt` — pass `speakerActions`; show error Snackbar.
- Modify `app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt` — suspend-call.
- Modify `app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt` — pass `speakerActions`.
- Modify `app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt` — add `speakerActions` to `Repositories`; wire `HttpSpeakerActions`.
- Modify `app/src/main/kotlin/com/sense/relay/RelayService.kt` — remove the control drain.
- Modify `app/src/main/kotlin/com/sense/relay/protocol/Messages.kt` — remove `NameSpeakerMsg`/`ReassignSpeakerMsg` + encode helpers.
- Test: `SpeakerRepositoryImplTest.kt`, `SessionDetailViewModelTest.kt`, `ChatViewModelTest.kt`; delete `SpeakerControlPortTest.kt`.

---

## Task 1: Server — `POST /speakers/{id}/rename` endpoint

**Files:**
- Modify: `server/src/sense_server/http/routes/speakers.py`
- Test: `server/tests/http/test_speakers.py`

**Interfaces:**
- Consumes: `app["sense_speaker_registry"]` (`SpeakerRegistry` with `.name(speaker_id, name)` and `.get(speaker_id)`); existing `_speaker_to_wire`.
- Produces: `POST /speakers/{speaker_id}/rename` → `200 {"speaker": <wire>}`; 400/404/409.

- [ ] **Step 1: Write the failing tests** — append to `server/tests/http/test_speakers.py`:

```python
async def test_rename_requires_token(tmp_path):
    cli, _ = await _client(tmp_path, InMemorySpeakerRegistry(SpeakerConfig()))
    try:
        resp = await cli.post("/speakers/sp-1/rename", json={"name": "Sarah"})
        assert resp.status == 401
    finally:
        await cli.close()


async def test_rename_409_when_disabled(tmp_path):
    cli, token = await _client(tmp_path, None)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 409
        assert (await resp.json())["error"] == "speaker_recognition_disabled"
    finally:
        await cli.close()


async def test_rename_400_on_empty_name(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", "Sarah"))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


async def test_rename_404_unknown_speaker(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/nope/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 404
    finally:
        await cli.close()


async def test_rename_returns_updated_speaker_without_biometrics(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("sp-1", None))
    cli, token = await _client(tmp_path, reg)
    try:
        resp = await cli.post(
            "/speakers/sp-1/rename",
            json={"name": "Sarah"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 200
        body = await resp.json()
        sp = body["speaker"]
        assert sp["speakerId"] == "sp-1"
        assert sp["displayName"] == "Sarah"
        assert sp["enrollmentStatus"] == "confirmed"
        for key in ("centroid", "embeddingModel", "embedding_model", "dim"):
            assert key not in sp
    finally:
        await cli.close()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && .venv/bin/pytest tests/http/test_speakers.py -k rename -v`
Expected: FAIL (404 — route does not exist yet).

- [ ] **Step 3: Implement the DTO + route** — add to `server/src/sense_server/http/routes/speakers.py`. Add imports at top:

```python
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
```

Add the DTOs after `_speaker_to_wire`:

```python
class RenameSpeakerDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    name: str = Field(min_length=1, max_length=128)


class ReassignSpeakerDTO(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    fromSpeakerId: str = Field(min_length=1)
    toSpeakerId: str = Field(min_length=1)
    scope: Literal["all"] = "all"
```

Add a `_bad_request` helper and register + implement the route inside `add_routes`:

```python
def _bad_request(message: str) -> web.Response:
    return web.json_response({"error": "bad_request", "message": message}, status=400)


async def rename_speaker(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    if reg is None:
        return web.json_response({"error": "speaker_recognition_disabled"}, status=409)
    speaker_id = request.match_info["speaker_id"]
    try:
        body = await request.json()
        dto = RenameSpeakerDTO.model_validate(body)
    except Exception as e:
        return _bad_request(f"invalid request: {e}")
    try:
        reg.name(speaker_id, dto.name)
    except KeyError:
        return web.json_response({"error": "speaker_not_found"}, status=404)
    return web.json_response({"speaker": _speaker_to_wire(reg.get(speaker_id))})
```

In `add_routes`, add (register `/speakers/reassign` before the `{speaker_id}/rename` pattern — done in Task 2; for now add only rename):

```python
    app.router.add_post("/speakers/{speaker_id}/rename", rename_speaker)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && .venv/bin/pytest tests/http/test_speakers.py -k rename -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Run the full server suite to confirm no regression**

Run: `cd server && .venv/bin/pytest -q`
Expected: PASS (all green; was 792 before).

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/http/routes/speakers.py server/tests/http/test_speakers.py
git commit -m "feat(server): POST /speakers/{id}/rename endpoint"
```

---

## Task 2: Server — `POST /speakers/reassign` endpoint

**Files:**
- Modify: `server/src/sense_server/http/routes/speakers.py`
- Test: `server/tests/http/test_speakers.py`

**Interfaces:**
- Consumes: `app["sense_event_store"]`, `app["sense_atom_store"]`, `app["sense_speaker_registry"]`; `reassign_speaker(registry, events, atoms, from_id, to_id, scope)` from `memory.speaker_registry`.
- Produces: `POST /speakers/reassign` → `204`; 400/404/409.

- [ ] **Step 1: Extend the `_client` fixture for `atom_store`** — in `server/tests/http/test_speakers.py`, change the signature and body:

```python
async def _client(tmp_path, registry, atom_store=None):
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token,
        get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(),
        session_lifecycle=SessionLifecycle(),
        event_store=InMemoryEventStore(),
        atom_store=atom_store,
        speaker_registry=registry,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    return cli, token
```

Add imports at the top of the test file:

```python
from sense_server.memory.atom import MemoryAtom
from sense_server.memory.store import InMemoryAtomStore
from sense_server.events.model import CaptureEvent
from datetime import datetime, timezone
```

- [ ] **Step 2: Write the failing tests** — append to `server/tests/http/test_speakers.py`:

```python
async def test_reassign_requires_token(tmp_path):
    cli, _ = await _client(
        tmp_path, InMemorySpeakerRegistry(SpeakerConfig()), InMemoryAtomStore()
    )
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "all"},
        )
        assert resp.status == 401
    finally:
        await cli.close()


async def test_reassign_409_when_disabled(tmp_path):
    cli, token = await _client(tmp_path, None, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 409
    finally:
        await cli.close()


async def test_reassign_400_on_bad_scope(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    cli, token = await _client(tmp_path, reg, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "one"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 400
    finally:
        await cli.close()


async def test_reassign_404_unknown_speaker(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("a", "A"))
    cli, token = await _client(tmp_path, reg, InMemoryAtomStore())
    try:
        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "missing"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 404
    finally:
        await cli.close()


async def test_reassign_204_rel_labels_events_and_atoms(tmp_path):
    reg = InMemorySpeakerRegistry(SpeakerConfig())
    reg.add_speaker(_speaker("a", "A"))
    reg.add_speaker(_speaker("b", "B", is_wearer=True))
    atoms = InMemoryAtomStore()
    events = InMemoryEventStore()
    # NOTE: build_app wires event_store=InMemoryEventStore() internally; to
    # assert relabeling we must use the SAME store the app uses. Re-build the
    # app by hand so the stores are the instances we seed:
    token = load_or_create_token(tmp_path / "tok")
    app = build_app(
        token=token, get_pubkey=lambda: bytes(32),
        session_index=SessionIndex(), session_lifecycle=SessionLifecycle(),
        event_store=events, atom_store=atoms, speaker_registry=reg,
    )
    server = TestServer(app)
    cli = TestClient(server)
    await cli.start_server()
    try:
        for seq in range(2):
            events.append(CaptureEvent(
                event_id=f"s:{seq}", session_id="s", seq=seq, kind="transcript",
                created_at=datetime(2026, 8, 1, tzinfo=timezone.utc), text="x",
                duration_ms=1000, start_ms=seq * 1000, speaker="a",
                speaker_confidence=0.9, speaker_assignment="confirmed"))
        atoms.append(MemoryAtom(
            atom_id="m1", session_id="s", source_event_id="s:0", kind="fact",
            text="y", created_at=datetime(2026, 8, 1, tzinfo=timezone.utc),
            start_ms=0, speaker="a", speaker_confidence=0.9,
            speaker_assignment="confirmed"))

        resp = await cli.post(
            "/speakers/reassign",
            json={"fromSpeakerId": "a", "toSpeakerId": "b", "scope": "all"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert resp.status == 204

        assert [e.speaker for e in events.events("s")] == ["b", "b"]
        assert atoms.atoms("s")[0].speaker == "b"
    finally:
        await cli.close()
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd server && .venv/bin/pytest tests/http/test_speakers.py -k reassign -v`
Expected: FAIL (404 — route not registered).

- [ ] **Step 4: Implement the route** — in `server/src/sense_server/http/routes/speakers.py`, add:

```python
async def reassign_speaker_route(request: web.Request) -> web.Response:
    reg = _registry(request.app)
    store = request.app.get("sense_event_store")
    atoms = request.app.get("sense_atom_store")
    if reg is None or store is None or atoms is None:
        return web.json_response({"error": "speaker_recognition_disabled"}, status=409)
    try:
        body = await request.json()
        dto = ReassignSpeakerDTO.model_validate(body)
    except Exception as e:
        return _bad_request(f"invalid request: {e}")
    if reg.get(dto.from_speaker_id) is None or reg.get(dto.to_speaker_id) is None:
        return web.json_response({"error": "speaker_not_found"}, status=404)
    from sense_server.memory.speaker_registry import reassign_speaker

    reassign_speaker(reg, store, atoms, dto.from_speaker_id, dto.to_speaker_id, dto.scope)
    return web.Response(status=204)
```

In `add_routes`, register reassign **before** the rename pattern (literal path first):

```python
    app.router.add_post("/speakers/reassign", reassign_speaker_route)
    app.router.add_post("/speakers/{speaker_id}/rename", rename_speaker)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && .venv/bin/pytest tests/http/test_speakers.py -v`
Expected: PASS (all rename + reassign tests).

- [ ] **Step 6: Run the full server suite**

Run: `cd server && .venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/http/routes/speakers.py server/tests/http/test_speakers.py
git commit -m "feat(server): POST /speakers/reassign endpoint"
```

---

## Task 3: Android — HTTP client + repository layer

Add the HTTP methods and repository surface. `SpeakerActions` is NOT changed yet (still WS) — this task is purely additive and compiles standalone.

**Files:**
- Modify: `app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt`
- Test: `app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt`

**Interfaces:**
- Consumes: `SenseHttpClient` (OkHttp + `DtoJson`); `SpeakerDto` (existing, in `Dtos.kt`).
- Produces: `SenseHttpClient.renameSpeaker(speakerId, name): SpeakerDto`, `SenseHttpClient.reassignSpeaker(fromId, toId, scope)`; `SpeakerApi` + `SpeakerRepository` equivalents.

- [ ] **Step 1: Add request/response DTOs** — append to `app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt`:

```kotlin
@Serializable
data class RenameSpeakerRequestDto(val name: String)

@Serializable
data class ReassignSpeakerRequestDto(
    val fromSpeakerId: String,
    val toSpeakerId: String,
    val scope: String = "all",
)

@Serializable
data class RenameSpeakerResponseDto(val speaker: SpeakerDto)
```

- [ ] **Step 2: Add `renameSpeaker` + `reassignSpeaker` to `SenseHttpClient`** — in `app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt`, add imports:

```kotlin
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.RequestBody.Companion.toRequestBody
import com.sense.relay.http.dto.RenameSpeakerRequestDto
import com.sense.relay.http.dto.ReassignSpeakerRequestDto
import com.sense.relay.http.dto.RenameSpeakerResponseDto
```

Add a `JSON` media type in a companion object (or top-level private) and the two methods after `getSpeakers()`:

```kotlin
    /** POST /speakers/{id}/rename — set a speaker's display name; returns the updated speaker. */
    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(
                RenameSpeakerRequestDto.serializer(),
                RenameSpeakerRequestDto(name),
            ).toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/speakers/$speakerId/rename")
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "speaker $speakerId not found")
                if (resp.code == 409) throw HttpStatusException(409)
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(
                    RenameSpeakerResponseDto.serializer(),
                    resp.body?.string().orEmpty(),
                ).speaker
            }
        }

    /** POST /speakers/reassign — move all of fromId's turns/embeddings to toId (v1 scope=all). */
    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all"): Unit =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(
                ReassignSpeakerRequestDto.serializer(),
                ReassignSpeakerRequestDto(fromId, toId, scope),
            ).toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/speakers/reassign")
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "speaker not found")
                if (resp.code == 409) throw HttpStatusException(409)
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                // 204 No Content — nothing to parse.
            }
        }
```

Add the companion object with the media type (if a companion object already exists, add to it):

```kotlin
    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }
```

- [ ] **Step 3: Extend `SpeakerApi` + `HttpSpeakerApi` + `SpeakerRepository`** — in `app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt`:

```kotlin
interface SpeakerApi {
    suspend fun getSpeakers(): List<SpeakerDto>
    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto
    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all")
}

private class HttpSpeakerApi(private val client: suspend () -> SenseHttpClient) : SpeakerApi {
    override suspend fun getSpeakers(): List<SpeakerDto> = client().getSpeakers()
    override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
        client().renameSpeaker(speakerId, name)
    override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) =
        client().reassignSpeaker(fromId, toId, scope)
}
```

Add the repository methods (after `loadSpeakers`):

```kotlin
    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerEntry = withContext(io) {
        val dto = apiProvider().renameSpeaker(speakerId, name)
        SpeakerEntry(speakerId = dto.speakerId, name = dto.displayName, isWearer = dto.isWearer)
    }

    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all") = withContext(io) {
        apiProvider().reassignSpeaker(fromId, toId, scope)
    }
```

- [ ] **Step 4: Write the repository test** — in `app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt`, add a fake `SpeakerApi` recording the calls and assert the repo delegates. (Follow that file's existing `FakeSpeakerApi` pattern; if it lacks one, add a private fake class implementing all three `SpeakerApi` methods.) Add:

```kotlin
@Test fun renameSpeakerDelegatesToApi() = runTest {
    val api = object : SpeakerApi {
        var renamed: Pair<String, String>? = null
        override suspend fun getSpeakers(): List<SpeakerDto> = emptyList()
        override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
            SpeakerDto(speakerId = speakerId, displayName = name, isWearer = true).also { renamed = speakerId to name }
        override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {}
    }
    val repo = SpeakerRepository(api)
    val entry = repo.renameSpeaker("sp-1", "Sarah")
    assertEquals("Sarah", entry.name)
    assertEquals(true, entry.isWearer)
    assertEquals("sp-1" to "Sarah", api.renamed)
}

@Test fun reassignSpeakerDelegatesToApiWithScopeAll() = runTest {
    val api = object : SpeakerApi {
        var reassigned: Triple<String, String, String>? = null
        override suspend fun getSpeakers(): List<SpeakerDto> = emptyList()
        override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
            SpeakerDto(speakerId = speakerId)
        override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) {
            reassigned = Triple(fromId, toId, scope)
        }
    }
    val repo = SpeakerRepository(api)
    repo.reassignSpeaker("a", "b")
    assertEquals(Triple("a", "b", "all"), api.reassigned)
}
```

- [ ] **Step 5: Run the Android repo tests**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.SpeakerRepositoryImplTest"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/src/main/kotlin/com/sense/relay/http/dto/Dtos.kt \
        app/src/main/kotlin/com/sense/relay/http/SenseHttpClient.kt \
        app/src/main/kotlin/com/sense/relay/data/SpeakerApi.kt \
        app/src/test/kotlin/com/sense/relay/data/SpeakerRepositoryImplTest.kt
git commit -m "feat(android): speaker rename/reassign HTTP client + repository"
```

---

## Task 4: Android — switch ViewModels to HTTP-always

Make `SpeakerActions` `suspend`, add `HttpSpeakerActions`, wire DI, update both VMs (rename optimistic + revert-on-failure + error flow; reassign → `onRefresh`), update routes + VM tests, delete `SpeakerControlPortTest`. This is one compile-coherent commit.

**Files:**
- Modify: `app/src/main/kotlin/com/sense/relay/data/SpeakerActions.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt`
- Test: `app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt`, `app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt`
- Delete: `app/src/test/kotlin/com/sense/relay/data/SpeakerControlPortTest.kt`

**Interfaces:**
- Consumes: `SpeakerRepository.renameSpeaker`/`reassignSpeaker` (Task 3); `SpeakerCache.upsert`/`get`/`remove`.
- Produces: `SpeakerActions` (suspend); `HttpSpeakerActions(repo, io)`; `SpeakerCache.remove(id)`; `Repositories.speakerActions`.

- [ ] **Step 1: Add `SpeakerCache.remove`** — in `app/src/main/kotlin/com/sense/relay/data/SpeakerCache.kt`, add (mirror the existing `upsert`/`get` threading style):

```kotlin
    fun remove(speakerId: String) {
        // same lock/synchronization as upsert/get — match the file's existing pattern
        entries.remove(speakerId)
    }
```
(Read the file first and use its exact lock field name; do not introduce a new lock.)

- [ ] **Step 2: Make `SpeakerActions` suspend + add `HttpSpeakerActions`** — replace the body of `app/src/main/kotlin/com/sense/relay/data/SpeakerActions.kt`. Keep the `SpeakerControlPort` object for now (removed in Task 5) but it must now implement the suspend methods. Updated file:

```kotlin
package com.sense.relay.data

import com.sense.relay.protocol.NameSpeakerMsg
import com.sense.relay.protocol.ReassignSpeakerMsg
import com.sense.relay.protocol.encode
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * The UI's port for renaming/reassigning speakers. Production wiring is
 * [HttpSpeakerActions] (HTTP-always — works for offline/historical sessions).
 * Tests inject a fake that records the calls.
 */
interface SpeakerActions {
    suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String)
    suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String = "all")
}

/**
 * HTTP-backed [SpeakerActions]. Delegates to [SpeakerRepository]; `sessionId`
 * is accepted for interface parity but ignored (v1 reassign is global).
 */
class HttpSpeakerActions(
    private val repo: SpeakerRepository,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) : SpeakerActions {
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) =
        withContext(io) { repo.renameSpeaker(speakerId, name) }
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) =
        withContext(io) { repo.reassignSpeaker(fromId, toId, scope) }
}

/** No-op default so VM tests that don't care about speaker actions can omit the arg. */
object NoopSpeakerActions : SpeakerActions {
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {}
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {}
}

/** Legacy WS queue — removed in Task 5; kept compiling here by implementing the suspend iface. */
object SpeakerControlPort : SpeakerActions {
    private val queue = ConcurrentLinkedQueue<String>()
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        queue.add(NameSpeakerMsg(session_id = sessionId, speaker_id = speakerId, name = name).encode())
    }
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
        queue.add(ReassignSpeakerMsg(session_id = sessionId, from_speaker_id = fromId, to_speaker_id = toId, scope = scope).encode())
    }
    fun poll(): String? = queue.poll()
    fun hasPending(): Boolean = !queue.isEmpty()
}
```

- [ ] **Step 3: Wire `HttpSpeakerActions` in DI** — in `app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`:
  - Add `val speakerActions: SpeakerActions,` to the `Repositories` data class (next to `speakerCache`).
  - In `init`, after `val speakerRepository = SpeakerRepository.fromClient(clientProvider)` (line ~153), add:

```kotlin
        val speakerActions = HttpSpeakerActions(speakerRepository)
```

  - Add `speakerActions = speakerActions,` to the `Repositories(...)` constructor call (line ~156-169).

- [ ] **Step 4: Update `SessionDetailViewModel`** — in `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModel.kt`:
  - Add imports:

```kotlin
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.SenseLog
import kotlinx.coroutines.launch
```
(Confirm `SenseLog`'s package by reading its existing usage in `ChatViewModel.kt` and use that import.)

  - Change the `speakerActions` constructor param default from `= SpeakerControlPort` to `= NoopSpeakerActions` (do NOT remove the default — several existing tests construct the VM without `speakerActions`), and remove the `SpeakerControlPort` import.
  - Add an error flow near `_isRefreshing`:

```kotlin
    private val _speakerError = MutableStateFlow<String?>(null)
    val speakerError: StateFlow<String?> = _speakerError.asStateFlow()
```

  - Replace `renameSpeaker`:

```kotlin
    fun renameSpeaker(speakerId: String, name: String) {
        val prior = speakerCache.get(speakerId)
        val isWearer = prior?.isWearer ?: false
        speakerCache.upsert(speakerId, name, isWearer)
        viewModelScope.launch {
            runCatching { speakerActions.nameSpeaker(id.value, speakerId, name) }
                .onFailure {
                    SenseLog.e(tag = "SessionDetail", msg = "renameSpeaker failed: ${it.javaClass.simpleName}", t = it)
                    if (prior != null) speakerCache.upsert(speakerId, prior.name, prior.isWearer)
                    else speakerCache.remove(speakerId)
                    _speakerError.value = "Couldn't rename on the server"
                }
                .onSuccess { _speakerError.value = null }
        }
    }
```

  - Replace `reassignSpeaker`:

```kotlin
    fun reassignSpeaker(fromId: String, toId: String) {
        viewModelScope.launch {
            runCatching { speakerActions.reassignSpeaker(id.value, fromId, toId) }
                .onSuccess { onRefresh() }
                .onFailure {
                    SenseLog.e(tag = "SessionDetail", msg = "reassignSpeaker failed: ${it.javaClass.simpleName}", t = it)
                    _speakerError.value = "Couldn't reassign on the server"
                }
        }
    }
```

- [ ] **Step 5: Update `SessionDetailRoute` to pass `speakerActions` + show the error** — in `app/src/main/kotlin/com/sense/relay/ui/recordings/SessionDetailScreen.kt`, in `SessionDetailRoute` (line ~54-64), add `speakerActions = RepositoryModule.repos.speakerActions,` to the `SessionDetailViewModel(...)` call. Add `val speakerError by vm.speakerError.collectAsState()` and pass `speakerError = speakerError` + `onDismissError = { /* clear handled below */ }` into `SessionDetailScreen`. In `SessionDetailScreen`, add a `SnackbarHost` + `LaunchedEffect(speakerError)` that shows + clears the message when non-null (small, follows the file's existing Compose idioms).

- [ ] **Step 6: Update `ChatViewModel.nameSpeaker`** — in `app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt`:
  - Add `import androidx.lifecycle.viewModelScope` and `import kotlinx.coroutines.launch` if not present.
  - Change the `speakerActions` constructor param default from `= com.sense.relay.data.SpeakerControlPort` to `= com.sense.relay.data.NoopSpeakerActions` (keep the default — some tests omit the arg).
  - Replace `nameSpeaker`:

```kotlin
    fun nameSpeaker(speakerId: String, name: String) {
        val sid = currentSessionId()
        if (sid.isNullOrEmpty()) return
        viewModelScope.launch {
            runCatching { speakerActions.nameSpeaker(sid, speakerId, name) }
                .onFailure { SenseLog.e(tag = "ChatViewModel", msg = "name_speaker send failed: ${it.javaClass.simpleName}", t = it) }
        }
    }
```

- [ ] **Step 7: Update `ChatRoute`** — in `app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt`, add `speakerActions = RepositoryModule.repos.speakerActions,` to the `ChatViewModel(...)` call (line ~38-42).

- [ ] **Step 8: Update VM tests** — in `app/src/test/kotlin/com/sense/relay/ui/recordings/SessionDetailViewModelTest.kt`:
  - Make `RecordingSpeakerActions`'s overrides `suspend fun`.
  - In `renameSpeakerEmitsNameSpeakerControlAndUpdatesCacheOptimistically`, after `vm.renameSpeaker("spk-1", "Sarah")` add `testScheduler.advanceUntilIdle()` before the assertions (the call is now async on `viewModelScope`).
  - In `reassignSpeakerEmitsReassignControlWithScopeAll`, add `testScheduler.advanceUntilIdle()` after `vm.reassignSpeaker("spk-1", "spk-2")`.
  - In the You-confirmation test, after `vm.confirmYou("Kevin")` add `testScheduler.advanceUntilIdle()`.
  - Add a new test `renameSpeakerRevertsCacheOnFailure` using a `SpeakerActions` that throws from `nameSpeaker`, asserting the cache returns to the prior name and `speakerError` becomes non-null.
  - Add a new test `reassignSpeakerRefreshesOnSuccess` using a `MutableStateFlow` events flow (like `onRefreshReFetches…`), asserting that after `vm.reassignSpeaker("a","b")` + `advanceUntilIdle()`, the events were re-fetched (the flow re-collected the updated value) — proving `onRefresh` fired.

In `app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt`: make any `SpeakerActions` fake's overrides `suspend fun`, and add `testScheduler.advanceUntilIdle()` after `nameSpeaker` calls. If the test relied on the `SpeakerControlPort` default, pass an explicit fake.

- [ ] **Step 9: Delete `SpeakerControlPortTest.kt`**

Run: `cd android/sense-relay && rm app/src/test/kotlin/com/sense/relay/data/SpeakerControlPortTest.kt`
(`SpeakerControlPort` itself is removed in Task 5; its test is deleted now to avoid maintaining a test for a dead path.)

- [ ] **Step 10: Run the Android unit tests**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest`
Expected: PASS (302+ tests, adjusted for the added/removed tests).

- [ ] **Step 11: Commit**

```bash
git add -A app/src/main/kotlin app/src/test/kotlin
git commit -m "feat(android): switch speaker rename/reassign to HTTP-always"
```

---

## Task 5: Android — retire the WS send path

Now that production VMs use `HttpSpeakerActions`, remove the dead WS outbound path.

**Files:**
- Modify: `app/src/main/kotlin/com/sense/relay/data/SpeakerActions.kt` (remove `SpeakerControlPort`)
- Modify: `app/src/main/kotlin/com/sense/relay/RelayService.kt`
- Modify: `app/src/main/kotlin/com/sense/relay/protocol/Messages.kt`

**Interfaces:**
- Consumes: Task 4 removed all production references to `SpeakerControlPort` and the drain.
- Produces: no WS outbound control frames.

- [ ] **Step 1: Confirm no remaining references** — Run:

```bash
cd android/sense-relay && grep -rn "SpeakerControlPort\|NameSpeakerMsg\|ReassignSpeakerMsg\|sendPendingSpeakerControls\|controlDrainJob" app/src/main --include="*.kt"
```
Expected: only the definition sites (SpeakerActions.kt, RelayService.kt, Messages.kt). Any other hit must be removed first.

- [ ] **Step 2: Remove `SpeakerControlPort`** — in `app/src/main/kotlin/com/sense/relay/data/SpeakerActions.kt`, delete the `SpeakerControlPort` object and the now-unused imports (`NameSpeakerMsg`, `ReassignSpeakerMsg`, `encode`, `ConcurrentLinkedQueue`). The file keeps only `SpeakerActions` + `HttpSpeakerActions`.

- [ ] **Step 3: Remove the drain from `RelayService`** — in `app/src/main/kotlin/com/sense/relay/RelayService.kt`:
  - Delete the `controlDrainJob: Job?` field (line ~98) and its doc.
  - Delete the `controlDrainJob?.cancel()` + `controlDrainJob = serviceScope.launch { … sendPendingSpeakerControls() … }` block in `onStartCommand` (line ~132-141).
  - Delete the `sendPendingSpeakerControls()` method (line ~265-272).
  - Remove now-unused imports.

- [ ] **Step 4: Remove the WS message classes** — in `app/src/main/kotlin/com/sense/relay/protocol/Messages.kt`, delete `NameSpeakerMsg`, `ReassignSpeakerMsg`, and their two `encode()` extension functions (line ~50-73). **Do NOT** touch the `propose` nudge parser below (line ~75+), which reads `kind == "name_speaker"` from a proactive payload — that is a different path.

- [ ] **Step 5: Build + test**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest`
Expected: PASS (compiles cleanly; no remaining references).

- [ ] **Step 6: Commit**

```bash
git add -A app/src/main/kotlin app/src/test/kotlin
git commit -m "refactor(android): retire WS speaker control-frame send path (HTTP-always)"
```

---

## Task 6: Server — retire the WS handlers + message types

**Files:**
- Modify: `server/src/sense_server/gateway/core.py`
- Modify: `server/src/sense_server/protocol/messages.py`
- Modify: `server/tests/gateway/test_core.py`
- Check: `server/tests/gateway/test_speaker_rollout.py`

**Interfaces:**
- Consumes: Tasks 1–2 (HTTP endpoints are the only rename/reassign path).
- Produces: server no longer parses `name_speaker`/`reassign_speaker` WS control frames.

- [ ] **Step 1: Confirm what references the WS types** — Run:

```bash
cd server && grep -rn "NameSpeaker\|ReassignSpeaker\|ReassignScope\|_on_name_speaker\|_on_reassign_speaker" src tests --include="*.py"
```
Expected: `protocol/messages.py`, `gateway/core.py`, `tests/gateway/test_core.py`, and possibly `tests/gateway/test_speaker_rollout.py`. The `registry.name` / `reassign_speaker` free function stay (used by HTTP).

- [ ] **Step 2: Remove the server WS handlers** — in `server/src/sense_server/gateway/core.py`:
  - Remove the `NameSpeaker` and `ReassignSpeaker` imports (lines ~41, 43).
  - Remove the two `isinstance` dispatch branches (lines ~183-186).
  - Remove `_on_name_speaker` (lines ~290-298) and `_on_reassign_speaker` (lines ~300-315).

- [ ] **Step 3: Remove the WS message types** — in `server/src/sense_server/protocol/messages.py`:
  - Delete `ReassignScope` (line ~49), `NameSpeaker` (lines ~52-58), `ReassignSpeaker` (lines ~61-74).
  - Update the `Inbound` union (line ~77-80) to `Union[Hello, Bye, CommandAck]`.
  - Remove now-unused imports (`Literal`/`Field` only if unused elsewhere — check before removing).

- [ ] **Step 4: Remove the WS-handler tests** — in `server/tests/gateway/test_core.py`, delete `test_name_speaker_control_calls_registry_name` (line ~440) and `test_reassign_speaker_control_rel_labels_events_and_atoms` (line ~452), and any shared fixture they uniquely need. In `server/tests/gateway/test_speaker_rollout.py`, remove any test that sends a `NameSpeaker`/`ReassignSpeaker` WS frame (keep tests that exercise the registry/identifier directly).

- [ ] **Step 5: Run the full server suite**

Run: `cd server && .venv/bin/pytest -q`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add server/src/sense_server/gateway/core.py server/src/sense_server/protocol/messages.py server/tests/gateway/test_core.py server/tests/gateway/test_speaker_rollout.py
git commit -m "refactor(server): retire WS name_speaker/reassign_speaker handlers (HTTP-always)"
```

---

## Task 7: Full suites green + manual smoke

- [ ] **Step 1: Full server suite**

Run: `cd server && .venv/bin/pytest -q`
Expected: PASS (all tests green).

- [ ] **Step 2: Full Android suite**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest`
Expected: PASS.

- [ ] **Step 3: Manual smoke (operator, real hardware)** — with the gateway running and speaker recognition enabled, open a **historical** Recordings session (no live relay):
  - Rename a speaker → label updates; restart gateway → name persists (server is source of truth).
  - Reassign one speaker to another → the timeline relabels on refresh.
  - Confirm a nudge (`NameSpeakerBubble`) still works (inbound `propose` path untouched; response now goes over HTTP).

- [ ] **Step 4: Final commit if any docs touched**

```bash
git add docs/
git commit -m "docs: HTTP speaker rename/reassign notes"
```

---

## Self-Review (completed during planning)

- **Spec coverage:** rename endpoint (Task 1), reassign endpoint (Task 2), Android HTTP plumbing (Task 3), HTTP-always VM switch + revert/onRefresh + error (Task 4), WS-send retirement Android (Task 5), WS retirement server (Task 6), suites + smoke (Task 7). The inbound nudge path is explicitly preserved (Task 5 Step 4). Out-of-scope items (per-row scope, mint-new-speaker) are not implemented. ✓
- **Type consistency:** `SpeakerActions` is `suspend` in both the interface (Task 4) and the `HttpSpeakerActions`/`RecordingSpeakerActions`/`SpeakerControlPort` impls. `renameSpeaker` returns `SpeakerDto` on the client and `SpeakerEntry` at the repo, matching `SpeakerEntry(speakerId, name, isWearer)`. Server `reassign_speaker` is called with `(reg, store, atoms, from, to, scope)` matching the free-function signature at `speaker_registry.py:440`. The `scope: Literal["all"]` DTO enforces the v1-only constraint consistently with the spec's 400-on-non-`all` rule. ✓
- **No placeholders:** every code step contains real code; the two `SpeakerCache.remove` / `SenseLog` import notes instruct reading the existing file rather than guessing the lock field / package. ✓