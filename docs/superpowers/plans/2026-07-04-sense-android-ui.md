# Sense — Android Main-App UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan phase-by-phase. Each phase expands into a task brief (matching the repo's existing `task-N-*.md` pattern under `.superpowers/sdd/`).

**Goal:** Turn the existing `com.sense.relay` Android app from a one-screen setup wizard into a polished post-setup consumer UI on top of the working BLE↔WS relay, plus the server HTTP endpoints it needs (sessions, transcript, status) and a shared core/repository/nav/design-system layer that scales to AI memory, live transcription, and multimodal data.

**Architecture:** Repository layer above the existing `SenseHttpClient`; read-only `RelayController` (process-singleton `StateFlow<RelayState>`) written only by `RelayService`; typed `Destination` navigation; cohesive `ui/design/` component library on top of `core/ui/` tokens; sealed `CaptureEvent` domain model leaves headroom for future kinds. Server gains `/sessions`, `/sessions/{id}`, `/sessions/{id}/events`, `/status` — all auth-gated on the existing `--http-port`.

**Tech Stack:** Android (Kotlin 2.0.20, Jetpack Compose, Material 3, Coroutines/StateFlow, DataStore, OkHttp 4.12, Navigation-Compose 2.8 typed destinations, ProcessLifecycleOwner); Python server (aiohttp 3.10, Pydantic v2, websockets 13, pytest 8).

**Spec:** `docs/superpowers/specs/2026-07-04-sense-android-ui-design.md` (commits `1628a9c`, `adf7b0f`). This plan is its execution contract.

**Branching:** Work on a new branch `android-main-app-ui` based on current `main`. Each phase ends with at least one commit; each task brief ends with one commit. The plan's phases map 1:1 to commit prefixes: `theme(core|ui)`, `feat(relay):`, `feat(data):`, `feat(http):`, `feat(ui):`, `feat(server):`, `test:`.

---

## Global Constraints (every task implicitly includes these)

- **Existing code is load-bearing.** The setup wizard (`SetupActivity`, `SetupScreen`, `SetupViewModel`, `RealDeviceScanner`, `ProvisioningClient`), the relay brain (`RelaySession`), the BLE link (`SensorLink`), the WS bridge (`ServerSocket`), the HTTP client (`SenseHttpClient`), the DataStore (`ServerConfig` + `StoreHolder`), and the manifests/gradle files are **not rewritten** — they are extended in place. A reviewer rejects a PR that restructures working code without cause.
- **Manual DI, no Hilt/Koin.** Process-singleton wiring in `RepositoryModule` (top-level `object`), constructed in `Application.onCreate()`. Matches the existing `StoreHolder` pattern.
- **No new Gradle modules.** Boundaries are package-level today; a future PR may lift `core/` or `data/` to a module when a feature needs it.
- **No disk cache for repositories in this slice.** In-memory `MutableStateFlow` per repository; resets across process death are fine.
- **Type-only contracts.** When a task "produces" an interface, the interface and its exception types are spelled out; the implementer cannot redefine the contract. If a later task finds a contract wrong, the correction is a one-line change to the interface plus updates to all call sites — do not silently widen the surface.
- **TDD where it pays.** Repositories, mappers, value classes, state machines, navigation — all written test-first. Composable UI and one-off wiring may be written directly and verified by the build (`./gradlew :app:assembleDebug`).
- **Tests live in `app/src/test/kotlin/com/sense/relay/...`** (host JVM). JUnit 4 (`org.junit.Test` + `org.junit.Assert.*`) for the existing tests; `kotlin.test.*` allowed for new ones. Match the surrounding file's style.
- **Build + test commands:** `./gradlew :app:assembleDebug :app:testDebugUnitTest`. Existing 45-task test baseline must remain green at every phase boundary.
- **Server tests:** `cd server && source .venv/bin/activate && python -m pytest -q`. Must remain 143-test-baseline-or-more green.
- **No new runtime dependencies** without a one-line justification in the task brief's "Files" section.

---

## Plan structure

Nine phases, ordered to keep every phase boundary producing a runnable, reviewable app:

| # | Phase                          | Boundary deliverable                                                      |
|---|--------------------------------|---------------------------------------------------------------------------|
| 1 | Foundation (theme + core + design + nav scaffold) | App still builds; theme is monochrome + tokens; `Destination` and `NavType` exist with one stub route. No business logic yet. |
| 2 | Repository layer + RelayController + ConfigurationRepository | Repositories compile with interfaces + a `RepositoryModule`; `RelayController` is wired into `RelayService`; existing wizard is unchanged. |
| 3 | Server endpoints (sessions, status) | Server pytest green; endpoints reachable with a bearer token. |
| 4 | Home + Dashboard | `MainActivity` shows Home with a real `DashboardRepository`; bottom bar renders. |
| 5 | Recordings + SessionDetail (with pagination + progressive events) | Recordings list paginates; SessionDetail shows the timeline live. |
| 6 | Device screen (relay + server state detail) | Device tab surfaces `RelayState` and `ServerStatus` decomposed. |
| 7 | Settings + re-provisioning flow | Settings edits config via `ConfigurationRepository`; "Reconfigure device" launches `SetupActivity` and refreshes on return. |
| 8 | Error handling, loading, empty states polish | Every screen has Loading/Empty/Error branches that match the design system. |
| 9 | Tests, manual smoke, final UX pass | All host tests + server pytests green; APK install + manual checklist run. |

Each phase is small enough to be reviewed in one sitting and ends with a commit. Within a phase, tasks are dispatched as separate subagent briefs (matching `.superpowers/sdd/task-N-*.md` style).

---

## File-structure map (locked in by the spec; reproduced here so the implementer does not have to re-derive it)

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/
├── core/                                       [NEW]
│   ├── model/
│   │   ├── PagedResult.kt                      [NEW]
│   │   ├── ApiError.kt                         [NEW]
│   │   └── Clock.kt                            [NEW]
│   ├── result/Outcome.kt                       [NEW]
│   ├── util/{FlowExt,DurationFmt}.kt           [NEW]
│   └── ui/{SenseTheme,Color,Type,Shape,Dimens}.kt [NEW]
├── relay/                                      [NEW]
│   ├── DeviceState.kt                          [NEW]
│   ├── RelayConnectionState.kt                 [NEW]
│   ├── ServerState.kt                          [NEW]
│   ├── RelayState.kt                           [NEW]
│   └── RelayController.kt                      [NEW]   (read-only outside RelayService)
├── http/
│   ├── SenseHttpClient.kt                      [EXTEND]
│   └── dto/{SessionSummaryDto,SessionDetailsDto,CaptureEventDto,ServerStatusDto,Mappers}.kt [NEW]
├── domain/model/{SessionId,SessionSummary,SessionDetails,CaptureEvent,TranscriptChunk,AudioSegment,DeviceSummary,ServerStatus}.kt [NEW]
├── data/                                       [NEW]
│   ├── ConfigurationRepository.kt              [NEW]
│   ├── SessionRepository.kt                    [NEW]
│   ├── DeviceRepository.kt                     [NEW]
│   ├── StatusRepository.kt                     [NEW]   (interface; polling impl follows)
│   ├── DashboardRepository.kt                  [NEW]
│   └── RepositoryModule.kt                     [NEW]
├── ui/
│   ├── MainActivity.kt                         [NEW]
│   ├── design/{StatePill,MetricCard,InfoRow,SectionHeader,EmptyState,LoadingCard,Skeleton,ConnectionBadge,AnimatedConnectionDot,Timeline,ListSection,SenseTopBar}.kt [NEW]
│   ├── nav/{Destination,NavType,AppNavigation,BottomBar}.kt [NEW]
│   ├── home/{HomeScreen,HomeViewModel,HomeUiState}.kt [NEW]
│   ├── recordings/{RecordingsScreen,RecordingsViewModel,RecordingsUiState,SessionDetailScreen,SessionDetailViewModel,SessionDetailUiState}.kt [NEW]
│   ├── device/{DeviceScreen,DeviceViewModel,DeviceUiState}.kt [NEW]
│   └── settings/{SettingsScreen,SettingsViewModel,SettingsUiState}.kt [NEW]
├── RelayService.kt                             [EXTEND] — write to RelayController
└── SenseApplication.kt                         [NEW]   — constructs RepositoryModule
```

Server additions:

```
server/src/sense_server/
├── http/routes/sessions.py                     [NEW]   (/sessions, /sessions/{id}, /sessions/{id}/events)
├── http/routes/status.py                       [NEW]   (/status)
├── sessions/index.py                           [NEW]   (in-memory session index; backed by SqliteEventStore)
└── http/app.py                                 [EXTEND] (register new routers)
server/tests/http/
├── test_sessions.py                            [NEW]
└── test_status.py                              [NEW]
```

---

## Phase 1 — Foundation (theme + core + design + nav scaffold)

**Objective:** Establish the visual + structural bones. No business logic, but every later phase drops into place on this skeleton.

**Files (key):**
- Create: `core/ui/Color.kt`, `core/ui/Type.kt`, `core/ui/Shape.kt`, `core/ui/Dimens.kt`, `core/ui/SenseTheme.kt` (move the existing `ui/Theme.kt` here or re-export).
- Create: `core/model/PagedResult.kt`, `core/model/ApiError.kt`, `core/model/Clock.kt`, `core/result/Outcome.kt`, `core/util/FlowExt.kt`, `core/util/DurationFmt.kt`.
- Create: `ui/design/SenseTopBar.kt`, `ui/design/StatePill.kt`, `ui/design/MetricCard.kt`, `ui/design/InfoRow.kt`, `ui/design/SectionHeader.kt`, `ui/design/EmptyState.kt`, `ui/design/LoadingCard.kt`, `ui/design/Skeleton.kt`, `ui/design/ConnectionBadge.kt`, `ui/design/AnimatedConnectionDot.kt`, `ui/design/Timeline.kt`, `ui/design/ListSection.kt`.
- Create: `ui/nav/Destination.kt`, `ui/nav/NavType.kt`, `ui/nav/BottomBar.kt`, `ui/nav/AppNavigation.kt` (4 stub composables).
- Create: `MainActivity.kt` (launcher? no — `SetupActivity` stays launcher; `MainActivity` is what `SetupActivity` launches on success), `SenseApplication.kt`.
- Modify: `AndroidManifest.xml` (add `android:name=".SenseApplication"`); keep `SetupActivity` as the LAUNCHER.

**Dependencies:** none beyond what's in the spec.

**Tasks (executed as subagent briefs):**
1.1 Move/extend `Theme.kt` to `core/ui/`. Monochrome Material 3 (no dynamic color). Tokens (Color, Type, Shape, Dimens) are the single source of truth. Host test: `SenseThemeTest` asserts dark/light choice by `isSystemInDarkTheme()`.
1.2 `core/model/PagedResult.kt` (sealed: `Loading`, `Page(items, nextCursor)`, `Exhausted`, `Error`). Host test: structural equality + map/flatMap helpers.
1.3 `core/result/Outcome.kt` (sealed: `Success<T>`, `Failure(ApiError)`); `core/model/ApiError.kt` (sealed). Host test: `Outcome.map`, `Outcome.fold`.
1.4 `core/util/DurationFmt.kt` (`formatHmMs`, `formatRelative`). Host test: zero/short/long/days.
1.5 `ui/design/` components — each is a pure `@Composable` over a state model (not over `Outcome<T>` directly; that mapping happens in screens). Skeletons and `LoadingCard` exist even if no screen uses them yet — they're the "we already have this" foundation.
1.6 `ui/nav/Destination.kt` (sealed interface) + `NavType.kt` (`SessionIdNavType` using `NavType.StringType` + a small parser). Host test: route round-trip, malformed-`SessionId` rejected.
1.7 `MainActivity.kt` (single-activity host) + `AppNavigation.kt` (4 stub composables + `BottomBar`) + `SenseApplication.kt` (constructs `RepositoryModule`; for now, an empty placeholder wiring — Phase 2 fills it in).

**Testing strategy:** host unit tests for every `core/` type and every `Destination`/`NavType`; `./gradlew :app:assembleDebug` for the theme + nav scaffold.

**Expected outcome:** App launches `SetupActivity`; on success it now launches `MainActivity` (instead of just `startForegroundService` and ending the wizard). `MainActivity` shows the monochrome theme, the bottom bar with 4 tabs, and a stub screen for each. No data yet.

**Rollback:** Phase 1 is purely additive. Removing the manifest entry + deleting new files returns to the previous wizard-only behavior.

---

## Phase 2 — Repository layer + RelayController + ConfigurationRepository

**Objective:** Land the data backbone that every later phase consumes. Repositories have no UI dependencies; ViewModels in later phases bind to them.

**Files:**
- Create: `domain/model/SessionId.kt` (value class wrapping `String`), `SessionSummary.kt`, `SessionDetails.kt`, `CaptureEvent.kt` (sealed), `TranscriptChunk.kt`, `AudioSegment.kt` (placeholder), `DeviceSummary.kt`, `ServerStatus.kt`.
- Create: `http/dto/` DTOs + `Mappers.kt`. **DTO ≠ domain**: a mapper function lives next to the DTOs and is the only path from wire to domain.
- Create: `data/ConfigurationRepository.kt` (interface + impl wrapping `ServerConfig`); `data/SessionRepository.kt` (interface; in-memory fake impl + a real impl calling `SenseHttpClient`); `data/DeviceRepository.kt` (interface + impl derived from `RelayController`); `data/StatusRepository.kt` (interface only; impl deferred to Phase 3 once endpoints exist); `data/RepositoryModule.kt` (object; `init(appContext)`).
- Create: `relay/{DeviceState,RelayConnectionState,ServerState,RelayState}.kt`, `relay/RelayController.kt` (process-singleton, `MutableStateFlow` private).
- Modify: `RelayService.kt` — at every state transition, write to `RelayController`. Existing listener methods call `controller.onDeviceConnected(...)`, `controller.onSocketOpen()`, etc. **No log-only paths**; the controller is the new observability surface.
- Create: `SenseApplication.kt` (final form): constructs `RepositoryModule.init(this)` and exposes the repositories via a top-level `Repositories` holder (singleton object).

**Dependencies:** Phase 1 (`core/model`, `core/result` exist).

**Tasks:**
2.1 Domain models with no Android imports. Host tests for value-class equality + sealed subtype coverage.
2.2 DTOs + mappers. Host test: JSON → DTO → domain for every `SessionSummary`, `SessionDetails`, `TranscriptChunk` field. Round-trip is the spec.
2.3 `ConfigurationRepository`: `interface { fun observe(): Flow<Config>; suspend fun save(c: Config); suspend fun saveCredentials(url: String, token: String) }`. Implementation wraps `ServerConfig`. Host test uses an in-memory `ServerConfig` substitute (or the existing `ServerConfig(dir)` with a tmp dir) — same pattern as `ServerConfigTest`.
2.4 `SessionRepository` interface + in-memory fake impl (test fixture). Real impl is a stub returning `Outcome.Failure(ApiError.Unreachable)` until Phase 3 lands endpoints. Host test: `PagedResult` paging semantics + `loadMore` append behavior.
2.5 `RelayController` (process singleton). `MutableStateFlow<RelayState>` private; public `StateFlow<RelayState>`; explicit `onDevice*`, `onSocket*`, `onError*` setters; `requestRefresh()` (re-binds the service). Host test: every transition emits exactly once; external writes are impossible (interface does not expose them).
2.6 `RelayService` writes: every existing `Log.i(...)` that marks a state change is replaced (or augmented) with a `controller.onX()` call. `SetupActivity` does **not** change.
2.7 `RepositoryModule` wires the implementations. Host test: module construction is pure (no I/O); swapping a fake is a one-line change.

**Testing strategy:** host unit tests for every repository, every mapper, every `RelayController` transition. `RelaySessionTest` must still pass (the relay brain is untouched).

**Expected outcome:** App still launches `SetupActivity` and runs identically. Internally, `RelayController` now mirrors relay state. `RepositoryModule` is process-warmed in `SenseApplication.onCreate`.

**Rollback:** `RelayService` is the only production-code change outside new files; revert that one file to restore the prior behavior.

---

## Phase 3 — Server endpoints (sessions, status)

**Objective:** Make the data the UI needs queryable from the phone. Server-only phase; no Android changes.

**Files (server):**
- Create: `server/src/sense_server/sessions/index.py` (in-memory `SessionIndex` derived from `EventStore.append` — interpose a callback so each `append` updates the index).
- Create: `server/src/sense_server/http/routes/sessions.py` (`GET /sessions`, `GET /sessions/{id}`, `GET /sessions/{id}/events`).
- Create: `server/src/sense_server/http/routes/status.py` (`GET /status`).
- Modify: `server/src/sense_server/http/app.py` (register both routers under the existing bearer-token middleware).
- Modify: `server/scripts/run_gateway.py` (build the `SessionIndex`, pass to `serve(...)` so the gateway updates it on every `event_store.append`).
- Create: `server/tests/http/test_sessions.py`, `server/tests/http/test_status.py`.

**Dependencies:** existing `EventStore` (`SqliteEventStore` + `InMemoryEventStore`), existing `bearer_auth_middleware`.

**Tasks:**
3.1 `SessionIndex`: pure, host-testable. Tracks per-session `start_at`, `ended_at`, `event_count`, `preview_text` (first non-empty transcript text). `record(event: CaptureEvent)` is the only mutator.
3.2 Hook the index into `GatewayCore._emit` (or a thin `EventStore.append` wrapper used by `SqliteEventStore` callers). The test seam: pass a `SessionIndex` into `GatewayCore`'s constructor alongside `EventStore`.
3.3 `/sessions` route: returns summaries ordered by `start_at desc`, supports `?limit=N&cursor=…` (cursor = base64-encoded `(before_iso, last_id)`; opaque to clients). `/sessions/{id}/events` returns the events list. `/sessions/{id}` returns summary + events together (used by `SessionDetailViewModel` when it doesn't already have the events).
3.4 `/status` route: returns `server.reachable=true`, `server.version`, `server.uptime_s`, `server.active_sessions` (count of open `GatewayCore`s in the WS handler — needs a tiny registry), `server.total_sessions`, `server.recent_events_24h`. `device: null` in this slice; the phone-side `DeviceRepository` covers device state.
3.5 Tests: `/sessions` returns 401 without token; returns empty list with token + no events; returns one summary per session after a `Hello→Bye` in the existing `tests/test_ws_auth.py`-style fixtures. `/status` requires token; counters update as events arrive.

**Testing strategy:** `python -m pytest -q` must remain green; new tests should bring the count to 160+ (we add ~10–15 endpoint tests + index tests).

**Expected outcome:** `curl -H "Authorization: Bearer <token>" http://host:8766/sessions` returns JSON. Endpoints are not yet wired into the Android app — that happens in Phase 4/5/6 as repositories gain real implementations.

**Rollback:** New routers; removing their `add_routes` calls restores prior surface.

---

## Phase 4 — Home + Dashboard (real data)

**Objective:** First feature screen. `Home` consumes `DashboardRepository` and renders a calm, glanceable state. The bottom bar becomes "real" — it routes to actual screens, even if some are still stubs.

**Files:**
- Modify: `data/DashboardRepository.kt` (real implementation; aggregates `RelayController.state`, `StatusRepository.observeStatus()`, `SessionRepository.observeSessions().first()` as a "recent" sample).
- Modify: `data/StatusRepository.kt` (gain a `PollingStatusRepository` impl — see "Polling lifecycle" below).
- Create: `ui/home/HomeScreen.kt`, `ui/home/HomeViewModel.kt`, `ui/home/HomeUiState.kt`.
- Modify: `MainActivity.kt` (route `Home` to `HomeScreen`).
- Modify: `ui/nav/AppNavigation.kt` (replace Home stub).

**Dependencies:** Phase 2 (repositories, `RelayController`); Phase 3 (`/sessions`, `/status` exist).

**Tasks:**
4.1 `PollingStatusRepository`: implements `StatusRepository`. On construction, takes a `SenseHttpClient` and a `lifecycle: Lifecycle` (caller passes `ProcessLifecycleOwner.get().lifecycle`). Internally:
   - Holds a `MutableStateFlow<Outcome<ServerStatus>?>` (`null` = no value yet).
   - `flow.onEach { … }.launchIn(scope)` where `scope` is a `CoroutineScope(SupervisorJob() + Dispatchers.Default)` and is started in `onStart`, cancelled in `onStop` via `lifecycle.addObserver`.
   - The polling loop: `while (isActive) { try { val s = client.getStatus(); _state.value = Outcome.Success(s) } catch (...) { _state.value = Outcome.Failure(...) }; delay(2_000) }`.
   - `observeStatus(): Flow<ServerStatus>` returns `_state.filterNotNull()` (caller gets the latest cached value immediately on subscribe; the polling loop is independent of subscribers).
4.2 `DashboardRepository` impl: `combine(relayController.state, statusRepo.observeStatus(), sessionRepo.observeSessions().map { it.items.take(3) }) { relay, status, recent -> DashboardState(relay, status, recent) }.stateIn(scope, SharingStarted.WhileSubscribed(5_000), DashboardState.Loading)`.
4.3 `HomeViewModel`: `val state = repo.observe().stateIn(viewModelScope, …)`. `HomeUiState` is a sealed `Loading | Loaded(dashboard) | Failed(reason)`.
4.4 `HomeScreen` composes `MetricCard`, `ConnectionBadge`, `AnimatedConnectionDot`, `SectionHeader`, `ListSection` (for "Recent sessions"), and an `EmptyState` if the recent list is empty.

**Testing strategy:** host tests for `PollingStatusRepository` (with a fake clock and a stubbed `SenseHttpClient`) — confirms 2s cadence, lifecycle pause/resume, cached value emission. Host tests for `DashboardRepository` (fakes for the three sources). `./gradlew :app:assembleDebug` for the screen.

**Polling lifecycle (locked decision):** Use `ProcessLifecycleOwner.get().lifecycle`. The repository is constructed in `SenseApplication.onCreate` and observes the process-wide lifecycle, so polling is foreground-only without per-Activity wiring. **Add `androidx.lifecycle:lifecycle-process` to `app/build.gradle.kts`** in this phase (one new dep, justified by polling semantics).

**Expected outcome:** Launching the app and completing setup shows a Home tab that reflects real relay + server + recent-session state within ~2.5s of first emission.

**Rollback:** `HomeScreen` is a new composable; deleting the `composable<Destination.Home>` line in `AppNavigation` removes it.

---

## Phase 5 — Recordings + SessionDetail (pagination + progressive events)

**Objective:** The list + detail experience for sessions. This is the largest feature in the slice.

**Files:**
- Modify: `data/SessionRepository.kt` (real impl wired to `SenseHttpClient.listSessions/getSession/getSessionEvents`).
- Create: `ui/recordings/RecordingsScreen.kt`, `ui/recordings/RecordingsViewModel.kt`, `ui/recordings/RecordingsUiState.kt`, `ui/recordings/SessionDetailScreen.kt`, `ui/recordings/SessionDetailViewModel.kt`, `ui/recordings/SessionDetailUiState.kt`.
- Modify: `ui/nav/AppNavigation.kt` (route `Recordings` + `Destination.SessionDetail(id)`).

**Dependencies:** Phase 2 (`SessionRepository` interface); Phase 3 (endpoints); Phase 4 (`PollingStatusRepository` cadence convention).

**Tasks:**
5.1 Real `SessionRepository` impl: `observeSessions()` = `flow { emit(PagedResult.Loading); val first = client.listSessions(limit = 20); emit(PagedResult.Page(first.items.map { it.toDomain() }, first.nextCursor)) }`. `loadMore()` reads the `stateIn`'s current `nextCursor` and emits a new `Page` that appends. The Flow is built on top of an internal `MutableStateFlow<PagedResult<SessionSummary>>` so paging is replayable.
5.2 `RecordingsViewModel`: state is `RecordingsUiState`; observes the paged list; exposes `onLoadMore()` (triggered when the last visible item in the `LazyColumn` is rendered). `LazyColumn` is built once; `items(list, key = { it.id.value })` is the only list.
5.3 `RecordingsScreen`: `ListSection` of `MetricCard`-like rows showing `id` (short hash), `startMs`-derived relative time, `preview` (first transcript line), `eventCount`. Tap → navigate to `Destination.SessionDetail(id)`.
5.4 `SessionDetailViewModel`: `combine(sessionRepo.observeSession(id), sessionRepo.observeSessionEvents(id)) { summary, events -> ... }.stateIn(...)`. Emits `LoadedSummary` first, then `LoadedSummaryAndEvents` as soon as the events Flow completes its first emit. New emissions append to the events list (re-sort by `seq`). `SessionDetailUiState` is a sealed type.
5.5 `SessionDetailScreen`: `SenseTopBar` with back arrow, summary `MetricCard`, then `Timeline` of `CaptureEvent`s (a `LazyColumn` of timeline rows). Empty events shows `EmptyState`; loading shows `LoadingCard`. New chunks arriving cause the LazyColumn to grow; no full re-render.

**Testing strategy:** host tests for the real `SessionRepository` (with a fake `SenseHttpClient`); host tests for `RecordingsViewModel` paging semantics; host test for `SessionDetailViewModel` progressive emission (a fake repository that emits a summary, then a first batch, then a second batch — assert the ViewModel's state at each step).

**Expected outcome:** Recordings tab lists sessions; scrolling to the end triggers `loadMore`; tapping a row shows the detail screen with progressive event loading.

**Rollback:** Same as Phase 4 — pure additions; nav line removal reverts.

---

## Phase 6 — Device screen (relay + server state detail)

**Objective:** Diagnostic view of the relay + server state, decomposed by domain. Not the average user's first stop, but the screen that proves the state architecture works.

**Files:**
- Create: `ui/device/DeviceScreen.kt`, `ui/device/DeviceViewModel.kt`, `ui/device/DeviceUiState.kt`.
- Modify: `ui/nav/AppNavigation.kt`.

**Dependencies:** Phase 2 (`RelayController`, `StatusRepository`); Phase 4 (`HomeUiState` patterns).

**Tasks:**
6.1 `DeviceViewModel` exposes `RelayState` + `ServerStatus` independently. `DeviceUiState` is a data class (no sealed wrapper — both are independently loadable; one may be present while the other is loading).
6.2 `DeviceScreen` composes `InfoRow`s grouped by `SectionHeader` per domain: Device (BLE address, last-seen, name), Relay (current state, last error, session id if any), Server (reachable, authenticated, last polled). A `ConnectionBadge` at the top shows the composite state.

**Testing strategy:** host test for `DeviceViewModel` with a `MutableStateFlow<RelayState>` and a stub `StatusRepository`.

**Expected outcome:** Device tab shows structured relay + server state. Useful for bench bring-up and "why is the relay not connecting" diagnosis.

**Rollback:** Same as Phase 4.

---

## Phase 7 — Settings + re-provisioning flow

**Objective:** The only place that edits config. Reuses `SetupActivity` for re-provisioning.

**Files:**
- Create: `ui/settings/SettingsScreen.kt`, `ui/settings/SettingsViewModel.kt`, `ui/settings/SettingsUiState.kt`.
- Modify: `MainActivity.kt` (handle `ActivityResult` from `SetupActivity` — refresh `RepositoryModule` + call `RelayController.requestRefresh()`).
- Modify: `SetupActivity.kt` (on `onDone`, set result code + finish; previously it just `startForegroundService` and finished).

**Dependencies:** Phase 2 (`ConfigurationRepository`); Phase 4 (polling cadence means server status is fresh after a refresh).

**Tasks:**
7.1 `SettingsViewModel` exposes `observe(): Flow<Config>` and `save(c: Config)`. Edits the existing `ConfigurationRepository` — does not write DataStore directly.
7.2 `SettingsScreen` shows: server URL (read-only display; tapping copies it), token (read-only; tapping copies), device address (read-only), "Reconfigure device" button.
7.3 "Reconfigure device" launches `SetupActivity` via `ActivityResultContracts.StartActivityForResult()`. `MainActivity` observes the result; on `RESULT_OK`, calls `RepositoryModule.refresh(this)` and `RelayController.requestRefresh()` (which re-emits the current state and asks the service to re-bind).
7.4 `SetupActivity` change: on success, set `setResult(RESULT_OK)` before `finish()`. **Do not** change the provisioning logic itself.

**Testing strategy:** host test for `SettingsViewModel` (save round-trip). The activity-result flow is verified manually + by a small instrumentation test if a device is connected; not in scope for host tests.

**Expected outcome:** Settings tab shows config; "Reconfigure device" launches the existing wizard; returning to MainActivity shows fresh state within one poll cycle.

**Rollback:** Activity result handling is additive; `SetupActivity` change is one line.

---

## Phase 8 — Error handling, loading, empty states polish

**Objective:** Every screen has Loading / Empty / Error branches that match the design system. No raw exception text in the UI. No infinite spinners.

**Files:** every screen + every ViewModel + a new `core/ui/ErrorMapper.kt` (maps `ApiError` → user-facing string + tone).

**Dependencies:** all prior phases.

**Tasks:**
8.1 `core/ui/ErrorMapper.kt` + host test: each `ApiError` variant maps to a stable, user-facing message (e.g. `Unreachable` → "Can't reach the server"; `Unauthorized` → "Token rejected — sign in again"; `Http(503)` → "Server is starting up").
8.2 Every screen renders the matching `EmptyState`/`LoadingCard` for its loading/empty branches. ViewModels expose the branch as a sealed type so the screen can't miss a case (`when` is exhaustive).
8.3 Network failures during `SessionRepository` paging show a small inline error row with "Retry" — not a full-screen error.

**Testing strategy:** host tests for `ErrorMapper`. UI verification is `assembleDebug` + visual inspection.

**Expected outcome:** The app degrades gracefully with no server, with a 401, with a 503, with an empty session list.

**Rollback:** additive.

---

## Phase 9 — Tests, manual smoke, final UX pass

**Objective:** Make the slice shippable.

**Files:** `app/src/test/kotlin/com/sense/relay/...` (more host tests for the ViewModels not yet covered), `server/tests/...` (more endpoint coverage), `docs/...` (a one-page user-facing summary if needed).

**Dependencies:** all prior phases.

**Tasks:**
9.1 Host test coverage targets: every ViewModel has a happy-path + an error-path + a loading-path test. Every repository has a test using a fake `SenseHttpClient`. Every `core/` helper has a test. `RelayController` transitions are covered exhaustively.
9.2 Server test coverage: every new endpoint has a 401 test, a happy-path test, and a 503-or-equivalent failure test. `SessionIndex` is tested in isolation.
9.3 `./gradlew :app:assembleDebug :app:testDebugUnitTest` + `cd server && source .venv/bin/activate && python -m pytest -q`. Both must be green.
9.4 Manual smoke checklist (run on a real device or emulator):
   - Cold start: SetupActivity → wizard → MainActivity.
   - Home shows live state within 2.5s.
   - Recordings lists; load-more works; detail renders progressively.
   - Device shows decomposed state.
   - Settings → Reconfigure device → wizard → return → state refreshes.
   - Background the app for 60s; resume; polling resumes; cache shows last value.
   - Toggle wifi off; Home shows `Unreachable`; toggle on; Home shows `Authenticated` again.
9.5 Final review pass (use `superpowers:requesting-code-review`) on the diff against `main`.

**Expected outcome:** A `release`-buildable APK that demonstrates the full capability of the existing relay/server stack through a polished consumer UI.

**Rollback:** This phase is verification, not feature work; nothing to roll back beyond reverting the whole branch.

---

## Execution order (for the implementer)

The phases above are sequential: each unblocks the next. Subagent briefs within a phase are dispatched in task order. The branch is `android-main-app-ui`; the base is `main` at `adf7b0f`.

**Dependency between phases is strict.** Do not start Phase 4 until Phase 2's `RelayController` and `RepositoryModule` land — otherwise `DashboardRepository` is a house of cards. Do not start Phase 5 until Phase 3's `/sessions` endpoint is merged and the spec's session-cursor protocol is stable, otherwise tests for `SessionRepository` are vacuous.

**What this plan is *not*** — it does not enumerate every test, every Composable body, every gradle line. Those are written in the per-task briefs (one brief per task) and reviewed individually, matching the repo's `task-N-*.md` pattern under `.superpowers/sdd/`. The plan's job is to lock the architecture, the order, and the boundary deliverables so the briefs can be written and reviewed in parallel without re-litigating decisions.

---

**Status:** Plan written. Awaiting user choice on execution mode (subagent-driven vs inline) per the writing-plans skill's execution-handoff section.
