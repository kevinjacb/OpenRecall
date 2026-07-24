# Wire Memory + Commands Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close three independent wiring gaps so the live `run_gateway.py` + Android app actually (1) extract atoms from captured transcripts, (2) issue commands when the LLM emits `issue_command`, and (3) let the user browse real atoms in a real `MemoryScreen`.

**Architecture:** Three tiny wiring edits — no new components, no new dependencies. Server: thread an `ExtractionEnqueuer` through `GatewayCore`/`serve()` and pass `command_validator`/`command_guardrails`/`dispatcher` to the `Planner(...)` call in `run_gateway.py`. Android: add `memoryRepository` to `RepositoryModule`, build a real `MemoryRoute` with `viewModel(factory)`, and replace the stub `MemoryScreen` with a real search + list Composable. One new server wiring test, one new Android `MemoryRepositoryTest`, one new Android `MemoryRouteTest` (Robolectric-free; pure ViewModel + Composable smoke).

**Tech Stack:** Python 3.12 / pytest-asyncio (server) — existing. Kotlin 1.9 / Jetpack Compose / kotlinx-coroutines-test (Android) — existing. No new dependencies.

## Global Constraints

- **TDD throughout.** Every fix lands a failing test first, then the minimal wiring, then a green run. `main` stays green at every commit.
- **No new HTTP / DTO / wire types.** `MemoryApi`, `MemoryRepository`, `MemoryViewModel`, `MemoryAtomDto`, `MemorySearchResponseDto`, `SessionMemoryResponseDto` already exist; only the production wiring is missing.
- **INV-11** (`ui/` must not import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`) is enforced by `ArchitecturalInvariantsTest`. The new `MemoryRoute` / `MemoryScreen` must respect it.
- **Manual DI** — `viewModelFactory { initializer { ... } }` is the project's pattern; new `MemoryRoute` uses the same.
- **Frequent commits.** Every task ends with one or more commits. Use `feat(server): …` / `feat(android): …` / `test: …` prefixes consistent with the repo history.
- **No regressions.** Every existing test must remain green. The server is at 577+ tests; Android is at 236+ tests. Both grow.

## What this slice does NOT add

- **Compose UI snapshot tests.** Robolectric PR #4736 blocks host-JVM Compose UI tests in this sandbox; the project has no `androidTest` setup. `MemoryRouteTest` is a Robolectric-free smoke (ViewModel state assertions + the route-level Composable's signature check) — not a UI snapshot test. Real-device manual smoke per the project status.
- **P3 proactive trigger, P4 firmware executors, BLE status characteristic → `relay_connected` flag** — out of scope. This slice does not touch `ConstantCapabilityProvider` defaults; the `relay_connected=False` caveat is documented in the spec and accepted for this slice.
- **Real MemoryScreen polish** beyond minimum: no infinite scroll, no per-session filter chips, no fuzzy search. Plain search + list + tap-to-AtomDetail.
- **Pre-existing tech debt** (`SetupActivity.kt` INV-11 direct import on the allow-list) — untouched.

---

## File Structure

### Modified files (server)

```
server/src/sense_server/gateway/core.py
  — add `enqueuer: ExtractionEnqueuer | None = None` to GatewayCore.__init__
  — call self._enqueuer.enqueue(self._session_id) after every successful
    self._store.append(event) in _emit
server/src/sense_server/gateway/adapter.py
  — add `enqueuer` param to serve(); thread into GatewayCore
server/scripts/run_gateway.py
  — pass enqueuer=enqueuer to serve(...)
  — pass command_validator=CommandValidator(),
           command_guardrails=StrictCommandGuardrails(confidence_autonomous=...),
           dispatcher=dispatcher
    to Planner(...)
server/tests/gateway/test_extraction_wiring.py
  — add test_gateway_core_enqueues_after_emit
```

### New files (Android)

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/
  MemoryScreen.kt            — REPLACED: real Composable (was a stub)
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/
  MemoryRoute.kt             — REPLACED: real Route with viewModel(factory)
                                  (was a stub calling MemoryScreen directly)
android/sense-relay/app/src/test/kotlin/com/sense/relay/data/
  MemoryRepositoryTest.kt    — NEW: mirrors CommandRepositoryTest
android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/
  MemoryRouteTest.kt         — NEW: Robolectric-free smoke (ViewModel + screen)
```

### Modified files (Android)

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt
  — add memoryRepository: MemoryRepository to Repositories
  — construct via apiProvider (same shape as agentRepository)
```

---

## Task 1: Server — wire ExtractionEnqueuer through GatewayCore and serve()

**Files:**
- Modify: `server/src/sense_server/gateway/core.py` (add `enqueuer` param, call in `_emit`)
- Modify: `server/src/sense_server/gateway/adapter.py` (add `enqueuer` param to `serve`)
- Modify: `server/scripts/run_gateway.py` (pass `enqueuer` to `serve`)
- Test: `server/tests/gateway/test_extraction_wiring.py` (add `test_gateway_core_enqueues_after_emit`)

**Interfaces:**
- Consumes: existing `ExtractionEnqueuer` (in `server/src/sense_server/memory/extraction_worker.py:31`); existing `GatewayCore._emit` (in `core.py:133`).
- Produces: `GatewayCore(session_id, …, enqueuer: ExtractionEnqueuer | None = None)` — when the `_emit` path successfully appends an event, it calls `self._enqueuer.enqueue(self._session_id)` if the enqueuer is not None. `serve(..., enqueuer: ExtractionEnqueuer | None = None)` threads the enqueuer to each per-connection `GatewayCore`.

- [ ] **Step 1: Add a failing test that proves `GatewayCore._emit` enqueues**

Append this test to `server/tests/gateway/test_extraction_wiring.py` (at the bottom of the file). Read the existing file first to confirm the imports and helper functions.

```python
from sense_server.gateway.core import GatewayCore
from sense_server.gateway.adapter import build_pipeline_factory
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.extraction_worker import ExtractionEnqueuer, ExtractionWorker
from sense_server.memory.stages import (
    EmbeddingStage, ExtractionStage, IndexingStage, Pipeline, VersionStampStage,
)
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.store import InMemoryAtomStore
from sense_server.memory.extract import ExtractedMemory
from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.protocol.messages import Hello, Transcript

def _stub_factory():
    """A pipeline factory whose ingest() returns no transcripts so we can
    drive _emit directly without standing up whisper/opus."""
    def _factory(start_seq: int):
        from sense_server.ingest.pipeline import AudioIngestPipeline
        return AudioIngestPipeline(
            reassembler=None,  # not used in this test
            decoder=None,      # not used
            transcriber=None,  # not used
            hop_ms=1000, window_ms=5000, sample_rate=16000,
        )
    return _factory


def test_gateway_core_enqueues_after_emit():
    """After _emit stores a transcript event, the enqueuer should see
    the session id and the worker should drain it into an atom."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)

    class FixedExtractor:
        def extract(self, text: str) -> list[ExtractedMemory]:
            return [ExtractedMemory(kind="fact", text=text)]
    class FixedEmbedder:
        def embed(self, texts: list[str]) -> list[list[float]]:
            return [[1.0, 0.0, 0.0] for _ in texts]
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=lambda: __import__("datetime").datetime(2026, 7, 19, tzinfo=__import__("datetime").timezone.utc)),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=FixedEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    worker = ExtractionWorker(events=events, atoms=atoms, pipeline=pipeline, metrics=metrics)
    worker.replace_enqueuer(enq)

    core = GatewayCore(
        pipeline_factory=_stub_factory(),
        event_store=events,
        enqueuer=enq,            # NEW PARAM — this is the seam we're testing
    )
    core.on_control(Hello(session_id="s1", start_seq=0))

    # Drive _emit directly with a fake transcript. The real factory
    # requires whisper/opus; for this test we only care about the
    # enqueue side-effect.
    from sense_server.gateway.core import GatewayCore as _GC
    # _emit takes list[Transcript]; Transcript has text + duration_ms.
    from sense_server.ingest.pipeline import Transcript as _T
    _GC._emit(core, [_T(text="hello world", duration_ms=1500)])

    assert enq.qsize() == 1, f"expected 1 enqueued, got {enq.qsize()}"
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/gateway/test_extraction_wiring.py::test_gateway_core_enqueues_after_emit -v`
Expected: `TypeError: __init__() got an unexpected keyword argument 'enqueuer'`

- [ ] **Step 3: Add `enqueuer` to `GatewayCore.__init__` and call it in `_emit`**

In `server/src/sense_server/gateway/core.py`, modify the imports at the top of the file (around line 21) to add `TYPE_CHECKING` for `ExtractionEnqueuer`:

```python
from typing import Callable, Union, TYPE_CHECKING

if TYPE_CHECKING:
    from ..memory.extraction_worker import ExtractionEnqueuer
```

Then change the `GatewayCore.__init__` signature (line 50-57) to:

```python
    def __init__(
        self,
        pipeline_factory: PipelineFactory,
        event_store: EventStore | None = None,
        dispatcher: CommandDispatcher | None = None,
        session_index: SessionIndex | None = None,
        session_lifecycle: SessionLifecycle | None = None,
        enqueuer: "ExtractionEnqueuer | None" = None,
    ) -> None:
        self._factory = pipeline_factory
        self._store = event_store
        self._dispatcher = dispatcher
        self._session_index = session_index
        self._session_lifecycle = session_lifecycle
        self._enqueuer = enqueuer
        self._session_id: str | None = None
        self._pipeline: AudioIngestPipeline | None = None
        self._event_seq = 0  # per-session monotonic event index
        self._cum_ms = 0  # cumulative audio offset within the session
```

In `GatewayCore._emit` (line 133-173), after the successful `self._store.append(event)` and `self._session_index.record(event)` blocks (around line 160-161), add the enqueue call. Find this exact block:

```python
            stored = True
            if self._store is not None:
                stored = self._store.append(event)
            if stored and self._session_index is not None:
                self._session_index.record(event)
```

Change it to:

```python
            stored = True
            if self._store is not None:
                stored = self._store.append(event)
            if stored and self._session_index is not None:
                self._session_index.record(event)
            if stored and self._enqueuer is not None and self._session_id is not None:
                self._enqueuer.enqueue(self._session_id)
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/gateway/test_extraction_wiring.py::test_gateway_core_enqueues_after_emit -v`
Expected: PASS

- [ ] **Step 5: Add `enqueuer` to `serve()` in `adapter.py` and thread it to `GatewayCore`**

In `server/src/sense_server/gateway/adapter.py`, change the `serve()` signature (line 124-133) to:

```python
async def serve(
    pipeline_factory: PipelineFactory,
    host: str = "0.0.0.0",
    port: int = 8765,
    event_store: "EventStore | None" = None,
    dispatcher: "CommandDispatcher | None" = None,
    token: str | None = None,
    session_index: "SessionIndex | None" = None,
    session_lifecycle: "SessionLifecycle | None" = None,
    enqueuer: "ExtractionEnqueuer | None" = None,
) -> None:
```

(Add `ExtractionEnqueuer` to the `TYPE_CHECKING` block at the top — `from ..memory.extraction_worker import ExtractionEnqueuer`.)

Then change the `GatewayCore` construction inside `handler()` (line 166-172) to:

```python
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
            session_index=session_index,
            session_lifecycle=session_lifecycle,
            enqueuer=enqueuer,
        )
```

- [ ] **Step 6: Wire `enqueuer=enqueuer` in `run_gateway.py`**

In `server/scripts/run_gateway.py`, find the `serve(...)` call (around line 230-239). It currently looks like:

```python
            await serve(
                factory,
                host=args.host,
                port=args.port,
                event_store=store,
                dispatcher=dispatcher,
                token=token,
                session_index=session_index,
                session_lifecycle=session_lifecycle,
            )
```

Change it to:

```python
            await serve(
                factory,
                host=args.host,
                port=args.port,
                event_store=store,
                dispatcher=dispatcher,
                token=token,
                session_index=session_index,
                session_lifecycle=session_lifecycle,
                enqueuer=enqueuer,
            )
```

- [ ] **Step 7: Run the full server test suite and confirm green**

Run: `cd server && .venv/bin/python -m pytest -x`
Expected: all 577+ tests pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/gateway/core.py \
        server/src/sense_server/gateway/adapter.py \
        server/scripts/run_gateway.py \
        server/tests/gateway/test_extraction_wiring.py
git commit -m "fix(server): wire ExtractionEnqueuer through gateway hot path

After every successful event append, GatewayCore._emit now enqueues
the session id to the ExtractionEnqueuer. The worker (already started
by run_gateway.py) drains the queue and runs the pipeline, advancing
the per-session cursor. Without this seam the worker loop sat idle
forever, the atom table stayed empty, and POST /agent always
short-circuited to Refuse(NO_SUPPORTING_MEMORY).

- GatewayCore gains enqueuer: ExtractionEnqueuer | None = None.
- serve() threads enqueuer into each per-connection core.
- run_gateway.py passes the existing enqueuer through.
- New test_gateway_core_enqueues_after_emit asserts the seam."
```

---

## Task 2: Server — wire the planner command-path in `run_gateway.py`

**Files:**
- Modify: `server/scripts/run_gateway.py` (Planner constructor)

**Interfaces:**
- Consumes: existing `CommandValidator` (in `server/src/sense_server/agent/validator_command.py`); existing `StrictCommandGuardrails` (in `server/src/sense_server/agent/guardrails_command.py`); existing `dispatcher` (in the same file, around line 96).
- Produces: a `Planner` instance whose `command_validator`, `command_guardrails`, and `dispatcher` attributes are populated. The Planner's existing dispatch path (in `agent/planner.py:205-348`) handles the rest.

- [ ] **Step 1: Write a manual smoke check (no new test file — the integration test already exists)**

The integration test `server/tests/integration/test_command_issue.py:186-198` already proves the command path works when the three deps are passed. This task is purely a wiring edit; the test is the existing one. Run it to confirm it's still green BEFORE this task.

Run: `cd server && .venv/bin/python -m pytest tests/integration/test_command_issue.py -v`
Expected: PASS (already).

- [ ] **Step 2: Verify `run_gateway.py` currently fails the same path (sanity check)**

Run: `grep -n "command_validator\|command_guardrails" server/scripts/run_gateway.py`
Expected: no matches. (The grep returns nothing — confirming the bug.)

- [ ] **Step 3: Add the three planner dependencies to `run_gateway.py`**

In `server/scripts/run_gateway.py`, find the import block (lines 142-153). Add these three imports:

```python
    from sense_server.agent.validator_command import CommandValidator as _CommandValidator
    from sense_server.agent.guardrails_command import StrictCommandGuardrails as _StrictCommandGuardrails
```

(Use the underscored aliases because the file already imports other symbols; this keeps the local names unambiguous. The actual class names are `CommandValidator` and `StrictCommandGuardrails`.)

Then find the `Planner(...)` constructor (line 160-181). It currently ends with:

```python
        clock=SystemClock(),
        ids=UuidIdGenerator(),
    )
```

Add three more keyword arguments before the closing `)`:

```python
        clock=SystemClock(),
        ids=UuidIdGenerator(),
        command_validator=_CommandValidator(),
        command_guardrails=_StrictCommandGuardrails(
            confidence_autonomous=agent_config.guardrails.confidence_autonomous,
        ),
        dispatcher=dispatcher,
    )
```

- [ ] **Step 4: Re-run the integration test to confirm no regressions**

Run: `cd server && .venv/bin/python -m pytest tests/integration/test_command_issue.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x`
Expected: all 577+ tests pass.

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/scripts/run_gateway.py
git commit -m "fix(server): wire command path on the live Planner

The Planner(...) call in run_gateway.py was missing command_validator,
command_guardrails, and dispatcher. As a result, when the LLM emitted
issue_command, planner._dispatch_command hit the
'command_validator is None' guard and refused every command with
'command dispatch is not configured on this server'. Wire the
existing CommandValidator and StrictCommandGuardrails with the same
confidence_autonomous threshold used by the answer path, plus the
already-constructed CommandDispatcher.

Note: StrictCommandGuardrails checks DeviceResourceStatus.relay_connected
which defaults to False until the BLE status characteristic is wired
(P3). Commands issued without a connected device will be refused
with 'device is not connected to the relay' — accepted for this
slice and documented in the spec."
```

---

## Task 3: Android — wire `MemoryRepository` into `RepositoryModule`

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/MemoryRepositoryTest.kt` (new)

**Interfaces:**
- Consumes: existing `MemoryApi` (in `data/MemoryApi.kt`); existing `MemoryRepository` (in `data/MemoryRepository.kt`); existing `Repositories` data class in `RepositoryModule.kt:43-54`; existing `clientProvider` in `RepositoryModule.kt:150`.
- Produces: a `MemoryRepository` field on `Repositories`. Constructed via the same `apiProvider` factory as `CommandRepository` and `AgentRepository`. Once constructed, ViewModels that take a `MemoryRepository` can be instantiated by the project's `viewModelFactory` pattern.

- [ ] **Step 1: Write a failing `MemoryRepositoryTest`**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/MemoryRepositoryTest.kt`. Read `CommandRepositoryTest.kt` and `MemoryApi.kt` and `MemoryRepository.kt` first to confirm the public surface and JSON shapes.

```kotlin
package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.MemorySearchResponseDto
import com.sense.relay.http.dto.SessionMemoryResponseDto
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test

class MemoryRepositoryTest {

    @Test
    fun `search maps DTOs to domain MemoryAtoms`() = runTest {
        val repo = MemoryRepository(StubMemoryApi(
            searchResponse = """{"schema_version":"v1","request_id":"r1","retrieval_trace_id":"t1","audit_id":"a1","query":"hello","atoms":[{"schema_version":"v1","atom_id":"a1","session_id":"s1","kind":"fact","text":"hello","created_at":"2026-07-19T00:00:00Z","start_ms":0,"source_event_id":"e1","source_modality":"transcript","extraction_version":"v1","embedding_model":"bge","extractor_prompt_version":"v1"}],"returned_count":1}""",
        ))
        val result = repo.search("hello", null, 10) as MemoryOutcome.Success
        assertEquals(1, result.atoms.size)
        assertEquals("a1", result.atoms[0].atomId)
        assertEquals("hello", result.atoms[0].text)
    }

    @Test
    fun `sessionAtoms maps DTOs to domain MemoryAtoms`() = runTest {
        val repo = MemoryRepository(StubMemoryApi(
            sessionAtomsResponse = """{"schema_version":"v1","session_id":"s1","atoms":[],"returned_count":0}""",
        ))
        val result = repo.sessionAtoms("s1") as MemoryOutcome.Success
        assertEquals(0, result.atoms.size)
    }

    @Test
    fun `search on 500 returns Error with code INTERNAL_ERROR`() = runTest {
        val repo = MemoryRepository(FailingMemoryApi())
        try {
            val r = repo.search("x", null, 10)
            assertTrue("expected Error, got $r", r is MemoryOutcome.Error)
            assertEquals(ErrorCode.INTERNAL_ERROR, (r as MemoryOutcome.Error).code)
        } catch (e: Throwable) {
            fail("expected MemoryOutcome.Error, threw $e")
        }
    }
}

private class StubMemoryApi(
    val searchResponse: String = """{"schema_version":"v1","request_id":"r","retrieval_trace_id":"t","audit_id":"a","query":"","atoms":[],"returned_count":0}""",
    val sessionAtomsResponse: String = """{"schema_version":"v1","session_id":"","atoms":[],"returned_count":0}""",
) : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto {
        @Suppress("UNCHECKED_CAST")
        val cls = Class.forName("com.sense.relay.http.dto.MemorySearchResponseDto").kotlin
        val companion = cls.companionObjectInstance
            ?: throw IllegalStateException("no companion")
        val fromJson = companion.javaClass.getMethod("fromJson", String::class.java)
        return fromJson.invoke(companion, searchResponse) as MemorySearchResponseDto
    }
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto {
        @Suppress("UNCHECKED_CAST")
        val cls = Class.forName("com.sense.relay.http.dto.SessionMemoryResponseDto").kotlin
        val companion = cls.companionObjectInstance
            ?: throw IllegalStateException("no companion")
        val fromJson = companion.javaClass.getMethod("fromJson", String::class.java)
        return fromJson.invoke(companion, sessionAtomsResponse) as SessionMemoryResponseDto
    }
}

private class FailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}
```

(If `MemorySearchResponseDto` and `SessionMemoryResponseDto` do not expose a `fromJson(String)` companion method, replace the JSON parsing with `DtoJson.decodeFromString(MemorySearchResponseDto.serializer(), searchResponse)` — mirror what `MemoryApi.executeRequest` does. Verify by reading `http/dto/AgentDto.kt:48-90` first.)

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.data.MemoryRepositoryTest"`
Expected: compile error (test references `MemoryRepository` correctly but the wiring doesn't exist yet — but the test should still compile and fail on a stub. If it compiles cleanly, the test is meaningless; revisit the assertion.)

- [ ] **Step 3: Construct `MemoryRepository` in `RepositoryModule`**

In `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`, add `memoryRepository: MemoryRepository` to the `Repositories` data class. The existing definition (line 43-54) is:

```kotlin
    data class Repositories(
        val configuration: ConfigurationRepository,
        val session: SessionRepository,
        val device: DeviceRepository,
        val status: StatusRepository,
        val dashboard: DashboardRepository,
        val relayController: RelayController,
        val commandRepository: CommandRepository,
        // P2-answers user-facing surface (ChatScreen):
        val agentRepository: AgentRepository,
        val chatHistoryStore: ChatHistoryStore,
    )
```

Add one line:

```kotlin
        // P1 memory-browse surface (MemoryScreen):
        val memoryRepository: MemoryRepository,
```

In the `init(app)` method (line 70-127), after the `agentRepository` construction (line 104-109) and before the `chatHistoryStore` construction (line 114), add:

```kotlin
        val memoryRepository = MemoryRepository(
            apiProvider = {
                val c = clientProvider()
                MemoryApi(baseUrl = c.baseUrl, token = c.token, client = c.client)
            },
        )
```

And in the `repos = Repositories(...)` call (line 116-126), add the new field:

```kotlin
        repos = Repositories(
            configuration = configuration,
            session = session,
            device = device,
            status = status,
            dashboard = dashboard,
            relayController = RelayController,
            commandRepository = commandRepository,
            agentRepository = agentRepository,
            chatHistoryStore = chatHistoryStore,
            memoryRepository = memoryRepository,
        )
```

- [ ] **Step 4: Re-run `MemoryRepositoryTest` and confirm it passes**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.data.MemoryRepositoryTest"`
Expected: PASS.

- [ ] **Step 5: Run the full Android test suite**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest`
Expected: 236+ tests pass (this slice adds 3).

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/data/MemoryRepositoryTest.kt
git commit -m "feat(android): wire MemoryRepository into RepositoryModule

MemoryApi + MemoryRepository existed with passing unit tests but were
never constructed in production. Add memoryRepository to the
Repositories data class, build it via the same apiProvider factory
as CommandRepository and AgentRepository so a re-provision takes
effect on the next /memory call without rebuilding. 3 new tests in
MemoryRepositoryTest cover success / empty / 500 paths."
```

---

## Task 4: Android — replace `MemoryRoute` stub with a real route

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt` (full rewrite — was a stub)
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/MemoryRouteTest.kt` (new)

**Interfaces:**
- Consumes: existing `MemoryViewModel(repo: MemoryRepository)`; existing `RepositoryModule.repos.memoryRepository`; existing `MemoryScreen` (replaced in Task 5).
- Produces: a `MemoryRoute` Composable that builds a `MemoryViewModel` via the project's `viewModelFactory` pattern, collects `state`, and passes it to `MemoryScreen`.

- [ ] **Step 1: Write a failing `MemoryRouteTest`**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/MemoryRouteTest.kt`. Read `MemoryViewModelTest.kt` and `MemoryViewModel.kt` first to confirm the surface.

```kotlin
package com.sense.relay.ui.memory

import com.sense.relay.data.MemoryApi
import com.sense.relay.data.MemoryAtom
import com.sense.relay.data.MemoryOutcome
import com.sense.relay.data.MemoryRepository
import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.MemorySearchResponseDto
import com.sense.relay.http.dto.SessionMemoryResponseDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

@OptIn(ExperimentalCoroutinesApi::class)
class MemoryRouteTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() { Dispatchers.setMain(dispatcher) }
    @After
    fun tearDown() { Dispatchers.resetMain() }

    @Test
    fun `MemoryViewModel constructed with a working repo returns atoms on search`() = runTest(dispatcher) {
        val atoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-19", 0, "e1", "transcript", "v1", "bge", "v1"),
        )
        val vm = MemoryViewModel(MemoryRepository(StubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("hello")
        vm.search()
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
        assertEquals("a1", vm.state.value.atoms[0].atomId)
        // The route hands vm.state to MemoryScreen; the Composable-level
        // assertion is manual (no Compose test infra in this sandbox).
        assertNotNull(vm.state.value)
    }

    @Test
    fun `error path puts errorMessage in state`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(FailingMemoryApi()))
        vm.onQueryChanged("x")
        vm.search()
        advanceUntilIdle()
        assertTrue(vm.state.value.errorMessage != null)
    }
}

// --- Stubs (kept local; same shape as MemoryViewModelTest's helpers) ---

private class StubMemoryApi(
    val searchAtoms: List<MemoryAtom> = emptyList(),
    val sessionAtoms: List<MemoryAtom> = emptyList(),
) : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        MemorySearchResponseDto(
            schema_version = "v1",
            request_id = "r",
            retrieval_trace_id = "t",
            audit_id = "a",
            query = query,
            atoms = searchAtoms.map { it.toDto() },
            returned_count = searchAtoms.size,
        )
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        SessionMemoryResponseDto(
            schema_version = "v1",
            session_id = sessionId,
            atoms = sessionAtoms.map { it.toDto() },
            returned_count = sessionAtoms.size,
        )
}

private class FailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}

private fun MemoryAtom.toDto() = com.sense.relay.http.dto.MemoryAtomDto(
    schema_version = "v1",
    atom_id = atomId,
    session_id = sessionId,
    kind = kind,
    text = text,
    created_at = createdAt,
    start_ms = startMs,
    source_event_id = sourceEventId,
    source_modality = sourceModality,
    extraction_version = extractionVersion,
    embedding_model = embeddingModel,
    extractor_prompt_version = extractorPromptVersion,
)
```

(If `MemoryViewModelTest.kt` already has a `StubMemoryApi` / `FailingMemoryApi` / `toDto()` in the same package, delete the duplicates and reuse the existing helpers — copy-paste the imports to that file. The 5 existing tests in `MemoryViewModelTest` are unchanged.)

- [ ] **Step 2: Run the test and confirm it passes already (sanity check)**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.ui.memory.MemoryRouteTest"`
Expected: PASS (the ViewModel logic is already covered; the new test is the same code path with the new file's name).

- [ ] **Step 3: Replace the `MemoryRoute.kt` stub with a real route**

Read `ChatRoute.kt` first to mirror the `viewModelFactory` pattern. Then overwrite `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt` with:

```kotlin
package com.sense.relay.ui.memory

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.sense.relay.data.RepositoryModule

/**
 * Real entry point for the Memory screen. Builds a [MemoryViewModel]
 * from [RepositoryModule.repos.memoryRepository] (a config-aware
 * factory, so a re-provision in Settings takes effect on the next
 * search) and renders the stateless [MemoryScreen].
 *
 * The deep-link entry from ChatScreen's refuse-link navigates here
 * with no arguments. A future per-session filter (e.g.
 * `Destination.Memory.withSessionId(id)`) can read it from
 * `backStackEntry.arguments` without changing the route.
 */
@Composable
fun MemoryRoute(modifier: Modifier = Modifier) {
    val vm: MemoryViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                MemoryViewModel(
                    repo = RepositoryModule.repos.memoryRepository,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    MemoryScreen(
        state = state,
        onQueryChanged = vm::onQueryChanged,
        onSearch = vm::search,
        onAtomTap = { /* TODO: navigate to AtomDetail in a follow-up slice */ },
        modifier = modifier,
    )
}
```

- [ ] **Step 4: Compile-check (no Compose UI test — just `:app:compileDebugKotlin`)**

Run: `cd android/sense-relay && ./gradlew :app:compileDebugKotlin`
Expected: BUILD SUCCESSFUL. (The new `MemoryRoute` calls `MemoryScreen(state, onQueryChanged, onSearch, onAtomTap, modifier)` — the next task defines that exact signature; this step will only pass AFTER Task 5 lands. **Run step 4 only after Task 5 step 4 is complete**; defer the actual `compileDebugKotlin` run to Task 5 step 5.)

- [ ] **Step 5: Run the new test and confirm it passes**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.ui.memory.MemoryRouteTest"`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/MemoryRouteTest.kt
git commit -m "feat(android): wire real MemoryRoute with ViewModel

MemoryRoute was a one-liner stub that just rendered the stub
MemoryScreen. Replace it with a real route: build a MemoryViewModel
via the project's viewModelFactory pattern (same shape as ChatRoute
and CommandsRoute), collect state, and hand it to a real
MemoryScreen. atom-tap is a TODO for the follow-up slice that
realises AtomDetailScreen."
```

---

## Task 5: Android — replace `MemoryScreen` stub with a real Composable

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt` (full rewrite — was a stub)
- Read-only: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/design/` (reuse the design system: `EmptyState`, `SenseTopBar`, `Spacing`, `LoadingIndicator` if it exists; `LoadingIndicator` may be inline — confirm by listing the directory)

**Interfaces:**
- Consumes: the `MemoryState` data class from `MemoryViewModel.kt:78-84`; the design system.
- Produces: a stateless `MemoryScreen` Composable with this exact signature:

```kotlin
@Composable
fun MemoryScreen(
    state: MemoryState,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
    onAtomTap: (String) -> Unit,
    modifier: Modifier = Modifier,
)
```

- [ ] **Step 1: Inventory the design system**

Run: `ls android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/design/`
Expected output: a list including `EmptyState`, `SenseTopBar`, `LoadingIndicator` (or similar), `Spacing`. Read any unknown files to confirm the public surface before using them.

- [ ] **Step 2: Write a one-off smoke render in a test (Robolectric-free)**

This is a sanity check that the new Composable compiles and exposes the expected signature. Add a test to `MemoryRouteTest.kt`:

```kotlin
    @Test
    fun `MemoryScreen signature accepts state + callbacks`() {
        // We can't actually render Compose without Robolectric, but the
        // test still serves as a build-time check that the function
        // exists with the documented signature. If this compiles and
        // the route in Task 4 step 3 calls it, we're good.
        val state = MemoryState()
        // Function reference: must not throw NoSuchMethodError at link time.
        val f: @Composable (
            MemoryState, (String) -> Unit, () -> Unit, (String) -> Unit, Modifier
        ) -> Unit = MemoryScreen
        // The reference itself is the assertion. Body is intentionally empty.
        @Suppress("UNUSED_VARIABLE") val captured = f
    }
```

- [ ] **Step 3: Run the new smoke test and confirm it fails (no such function)**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.ui.memory.MemoryRouteTest.MemoryScreen signature accepts state + callbacks"`
Expected: compile error — `MemoryScreen` has the old stub signature, not the new one.

- [ ] **Step 4: Replace `MemoryScreen.kt` with the real Composable**

Overwrite `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt` with:

```kotlin
package com.sense.relay.ui.memory

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Search
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Stateless Memory screen. The Route owns the ViewModel; this Composable
 * only renders. Search is debounced inside the ViewModel; pressing the
 * search button calls [onSearch] which triggers the debounce.
 *
 * Atoms are listed in the order returned by the server (the scorer is
 * responsible for ranking). Each row shows the atom's kind chip +
 * truncated text + a relative timestamp placeholder. Tapping a row
 * fires [onAtomTap] for the future AtomDetail deep-link.
 *
 * INV-11: this Composable lives in `ui/memory/`. It must not import
 * any class under `com.sense.relay.http.*` or
 * `com.sense.relay.http.dto.*`. The architectural invariant test
 * (`ArchitecturalInvariantsTest`) enforces this.
 */
@Composable
fun MemoryScreen(
    state: MemoryState,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
    onAtomTap: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Memory"))
        SearchBar(
            query = state.query,
            onQueryChanged = onQueryChanged,
            onSearch = onSearch,
        )
        when {
            state.loading -> LoadingState()
            state.errorMessage != null -> ErrorState(message = state.errorMessage!!)
            state.atoms.isEmpty() -> EmptyMemoryState(query = state.lastQuery)
            else -> AtomList(atoms = state.atoms, onAtomTap = onAtomTap)
        }
    }
}

@Composable
private fun SearchBar(
    query: String,
    onQueryChanged: (String) -> Unit,
    onSearch: () -> Unit,
) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(horizontal = Spacing.md, vertical = Spacing.sm),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        OutlinedTextField(
            value = query,
            onValueChange = onQueryChanged,
            placeholder = { Text("Search memory…") },
            singleLine = true,
            modifier = Modifier
                .weight(1f)
                .testTag("memory_query"),
        )
        Spacer(Modifier.height(Spacing.xs))
        IconButton(
            onClick = onSearch,
            modifier = Modifier.testTag("memory_search"),
        ) {
            Icon(Icons.Filled.Search, contentDescription = "Search")
        }
    }
}

@Composable
private fun LoadingState() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.md),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        CircularProgressIndicator(modifier = Modifier.testTag("memory_loading"))
    }
}

@Composable
private fun ErrorState(message: String) {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.md),
        verticalArrangement = Arrangement.Center,
        horizontalAlignment = Alignment.CenterHorizontally,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodyLarge,
            color = MaterialTheme.colorScheme.error,
            modifier = Modifier.testTag("memory_error"),
        )
    }
}

@Composable
private fun EmptyMemoryState(query: String) {
    if (query.isNotEmpty()) {
        EmptyState(
            title = "No matches",
            body = "No memory atoms match \"$query\".",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty_no_match"),
        )
    } else {
        EmptyState(
            title = "Memory",
            body = "Search across your captured transcripts. " +
                "Type a query above and tap the search icon.",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty_initial"),
        )
    }
}

@Composable
private fun AtomList(
    atoms: List<com.sense.relay.data.MemoryAtom>,
    onAtomTap: (String) -> Unit,
) {
    LazyColumn(
        modifier = Modifier
            .fillMaxSize()
            .testTag("memory_list"),
        verticalArrangement = Arrangement.spacedBy(Spacing.xs),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(Spacing.md),
    ) {
        items(atoms, key = { it.atomId }) { atom ->
            AtomRow(atom = atom, onTap = { onAtomTap(atom.atomId) })
        }
    }
}

@Composable
private fun AtomRow(
    atom: com.sense.relay.data.MemoryAtom,
    onTap: () -> Unit,
) {
    Surface(
        shape = MaterialTheme.shapes.medium,
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = Modifier
            .fillMaxWidth()
            .testTag("memory_atom_${atom.atomId}"),
        onClick = onTap,
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text(
                text = atom.kind,
                style = MaterialTheme.typography.labelSmall,
            )
            Spacer(Modifier.height(Spacing.xxs))
            Text(
                text = atom.text,
                style = MaterialTheme.typography.bodyLarge,
                maxLines = 3,
                overflow = TextOverflow.Ellipsis,
            )
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = atom.createdAt,
                style = MaterialTheme.typography.labelSmall,
            )
        }
    }
}
```

(If `Spacing.xxs` / `Spacing.xs` constants don't exist in `core/ui/Spacing.kt`, replace them with `4.dp` / `8.dp` literal paddings. Confirm by reading `Spacing.kt` first.)

- [ ] **Step 5: Compile-check the project**

Run: `cd android/sense-relay && ./gradlew :app:compileDebugKotlin`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 6: Re-run the smoke test from Step 2; it should now compile and pass**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.ui.memory.MemoryRouteTest"`
Expected: PASS.

- [ ] **Step 7: Run the full Android test suite**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest`
Expected: 239+ tests pass (this slice adds 3 MemoryRepositoryTest + 2 MemoryRouteTest; the new 1 in MemoryRouteTest is the signature smoke; the other 1 is the same as the existing MemoryViewModelTest so they overlap — net 4 new tests).

- [ ] **Step 8: Confirm INV-11 still holds**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest --tests "com.sense.relay.arch.ArchitecturalInvariantsTest"`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/MemoryRouteTest.kt
git commit -m "feat(android): real MemoryScreen — search bar, atom list, states

Replace the one-line stub with a stateless Composable that:
- shows the SenseTopBar,
- renders a search field + button wired to onQueryChanged / onSearch,
- dispatches to loading / error / empty / populated states,
- lists atoms in a LazyColumn with kind + text + timestamp rows,
- fires onAtomTap for the future AtomDetail deep-link.

Pure UI; no data-layer changes. ArchitecturalInvariantsTest still
passes (no http/dto imports)."
```

---

## Task 6: Verify — server + Android full suites green, end-to-end smoke

**Files:** none — verification only.

- [ ] **Step 1: Run the server's full test suite**

Run: `cd server && .venv/bin/python -m pytest -x`
Expected: all 577+ tests pass.

- [ ] **Step 2: Run the Android test suite**

Run: `cd android/sense-relay && ./gradlew testDebugUnitTest`
Expected: 239+ tests pass.

- [ ] **Step 3: Update the project-status memory**

Edit `/Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/project-status.md` to reflect the closed slice. The new line under "What remains" should be the next-highest-value item (P3 proactive trigger or the real-device BLE bring-up). The 2026-07-17 → 2026-07-19 bullet list should gain a new entry for the three fixes.

- [ ] **Step 4: Manual smoke (post-onboarding)**

The user runs the gateway with `SENSE_LLM_MODEL` + `SENSE_EMBED_MODEL` set, captures a few minutes of audio, then:
- `curl -H 'Authorization: Bearer <token>' 'http://localhost:8766/memory?q=test'` → expects `atoms` to be non-empty.
- Open the app, Chat tab, ask a question → expects Answer (not Refuse) with atom chips.
- Tap "browse memory directly" on a refuse bubble → expects real Memory screen with atoms.
- `curl -X POST -H 'Authorization: Bearer <token>' -H 'Content-Type: application/json' -d '{"schema_version":"v1","session_id":"","text":"record a 3 second video","limit":10}' 'http://localhost:8766/agent'` → expects `outcome: "issue_command"` with a `command_id` and `command_status: "PENDING"`.

(These are out-of-scope for the agentic plan; document the smoke steps in the commit message of this verification task.)

- [ ] **Step 5: Final commit (memory update only — no code change)**

```bash
cd /Users/kevin/Projects/Sense
git add .claude/projects/-Users-kevin-Projects-Sense/memory/project-status.md
git commit -m "docs: project-status 2026-07-19 — close the wire-memory-and-commands slice"
```

---

## Self-Review

- **Spec coverage:** §2.1 enqueuer → Task 1. §2.2 planner command-path → Task 2. §2.3 Android wiring + UI → Tasks 3, 4, 5. §4 error handling → Task 1 (enqueuer None), Task 3 (not provisioned), Task 4 (search empty). §5 testing → Task 1 server test, Task 3 + 4 Android tests. §8 files → every file named. §9 verification gate → Task 6.
- **Placeholders:** none. (The `TODO: navigate to AtomDetail` in Task 4 step 3 is documented in the spec as out-of-scope; the comment makes the deliberate deferral explicit.)
- **Type consistency:** `MemoryScreen(state, onQueryChanged, onSearch, onAtomTap, modifier)` defined in Task 5 step 4 matches the call in Task 4 step 3. `Repositories.memoryRepository: MemoryRepository` (Task 3) matches the read in Task 4 step 3. `GatewayCore(enqueuer=...)` (Task 1 step 3) matches the call in Task 1 step 1 and the call in adapter step 5. `serve(..., enqueuer=...)` (Task 1 step 5) matches the call in Task 1 step 6.
- **One ambiguity I caught:** the `MemoryRepositoryTest` JSON deserialization. The stubs in this plan use reflection to find a `fromJson(String)` companion; if it doesn't exist, fall back to `DtoJson.decodeFromString(...)` — the plan calls this out explicitly in the parenthetical.
- **One sequencing constraint:** Task 4 step 4 (compile-check) and Task 5 step 5 (compile-check) are sequenced — Task 4's route references Task 5's `MemoryScreen` signature. The plan calls this out at Task 4 step 4.
