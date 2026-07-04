# Sense — Android Main App UI (post-setup interface slice)

**Date:** 2026-07-04
**Status:** Design, pending user review
**Scope:** Turn the existing Android app from a one-screen setup wizard into the
full Sense interface: a polished, post-setup consumer app on top of the working
relay/BLE/WS/HTTP infrastructure. Adds the server endpoints the UI needs to render
sessions, transcripts, and server status. Live tab is deferred (the phone is a
relay; it has no decoded audio to visualize).

## Context & decisions (locked in brainstorm)

- **The Android app is already a working relay.** The relay service, BLE link,
  WebSocket, HTTP auth/CA pinning, DataStore config, and Compose setup wizard are
  shipped and tested (see `2026-07-02-android-setup-pairing-design.md`). This slice
  **extends**, never rewrites, that slice.
- **The phone is the relay only.** The wearable streams Opus audio directly to the
  server (Mac) over the BLE-relayed WS. The phone has no decoded audio, no
  waveform, no mic capture. The UI reflects *relay + server state*, not audio
  visualization.
- **Server endpoints to add (this slice):** `GET /sessions`, `GET /sessions/{id}`,
  `GET /sessions/{id}/events`, `GET /status`. All auth-gated, same bearer token.
- **The domain thinks in *sessions*, not recordings.** A session is one
  Hello→Bye on the gateway WS; it is the durable unit of capture. Future kinds
  (summaries, AI memory, images, multimodal) join the same per-session timeline
  under the same id.
- **UX bar:** modern, simple, monochrome, generous spacing, large touch targets,
  super-fluid animations. This is now an opinionated product surface, not a
  demo.
- **State observability:** a process-singleton `RelayController` exposes a
  read-only `StateFlow<RelayState>`. The relay service is the only writer.

## Architecture refinements (locked in brainstorm)

1. **Repository layer.** UI → ViewModel → Repository → SenseHttpClient. No
   ViewModel touches HTTP directly.
2. **RelayController is read-only outside the service.** `MutableStateFlow`
   private; public surface is `StateFlow<RelayState>`.
3. **DashboardRepository.** Home consumes a single aggregated state.
4. **Domain models separated by responsibility** (SessionSummary, SessionDetails,
   CaptureEvent, TranscriptChunk, AudioSegment). Room left for future kinds.
5. **Typed navigation.** Sealed `Destination` + `NavType`; no raw strings.
6. **Separate state domains** (DeviceState, RelayConnectionState, ServerState).
   Compose at the edges.
7. **ConfigurationRepository** owns app config. SettingsViewModel edits through
   it; everyone else reads from it.
8. **Pagination-ready.** Repositories return `Flow<PagedResult<T>>` from day one
   even when the server returns a small list.
9. **Status is observable**, not polled. `StatusRepository.observeStatus()` —
   today a poll, tomorrow a stream.
10. **Expanded design system** with cohesive reusable components.
11. **One source of truth per state.** No duplicated state across ViewModels.
12. **Shared `core/` package** for cross-cutting infrastructure.

## Module / package layout

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/
├── core/                                       [NEW — package only, no gradle module]
│   ├── model/
│   │   ├── PagedResult.kt                      [NEW] — sealed: Loading, Page(items, nextCursor), Exhausted, Error
│   │   ├── ApiError.kt                         [NEW] — sealed: Unauthorized, Unreachable, Http(code), Unknown(throwable)
│   │   └── Clock.kt                            [NEW] — interface + SystemClock default; for testable time
│   ├── result/
│   │   └── Outcome.kt                          [NEW] — sealed: Success, Failure(ApiError); map/fold helpers
│   ├── util/
│   │   ├── FlowExt.kt                          [NEW] — stateIn/startWith/wrapOutcome helpers
│   │   └── DurationFmt.kt                      [NEW] — "00:14:23" / "2 min ago"
│   └── ui/
│       ├── SenseTheme.kt                       [NEW — re-export from ui/Theme.kt; tokens defined here]
│       ├── Color.kt                            [NEW] — monochrome palette tokens
│       ├── Type.kt                             [NEW] — Material 3 type scale
│       ├── Shape.kt                            [NEW] — rounded shapes
│       └── Dimens.kt                           [NEW] — spacing, sizing tokens
│
├── relay/                                      [NEW package]
│   ├── DeviceState.kt                          [NEW] — sealed: Unknown, Connected(addr, name), Scanning, Disconnected(reason)
│   ├── RelayConnectionState.kt                 [NEW] — sealed: Idle, BleScanning, BleConnected, SocketConnecting(addr), Live(sessionId, since), Reconnecting(after), Failed(reason)
│   ├── ServerState.kt                          [NEW] — sealed: Unknown, Reachable, Authenticated, Unreachable(reason), Syncing
│   ├── RelayState.kt                           [NEW] — data class composing the three above + lastError
│   ├── RelayController.kt                      [NEW] — process-singleton; private MutableStateFlow; public StateFlow<RelayState>
│   └── RelayService.kt                         [EXTEND] — write to controller on every transition; remove log-only paths
│
├── http/                                       [EXTEND]
│   ├── SenseHttpClient.kt                      [EXTEND] — add: listSessions, getSession, getSessionEvents, getStatus
│   └── dto/                                    [NEW]
│       ├── SessionSummaryDto.kt
│       ├── SessionDetailsDto.kt
│       ├── CaptureEventDto.kt
│       ├── ServerStatusDto.kt
│       └── Mappers.kt                          [NEW] — DTO → domain; keeps wire format separate from domain
│
├── domain/                                     [NEW package — Kotlin-only, no Android deps]
│   └── model/
│       ├── SessionSummary.kt
│       ├── SessionDetails.kt
│       ├── SessionId.kt                        [NEW — value class wrapping String]
│       ├── CaptureEvent.kt                     [NEW] — sealed; first kind TranscriptChunk
│       ├── TranscriptChunk.kt
│       ├── AudioSegment.kt                     [NEW — placeholder, unused in this slice]
│       ├── DeviceSummary.kt
│       └── ServerStatus.kt
│
├── data/                                       [NEW package — repositories]
│   ├── ConfigurationRepository.kt              [NEW] — wraps ServerConfig; exposes Flow<Config>
│   ├── SessionRepository.kt                    [NEW] — sessions, session details, events; paged
│   ├── DeviceRepository.kt                     [NEW] — derives DeviceSummary from RelayController
│   ├── StatusRepository.kt                     [NEW] — observeStatus() -> Flow<ServerStatus>; today polls
│   ├── DashboardRepository.kt                  [NEW] — aggregates Relay + Server + Recent Sessions
│   └── RepositoryModule.kt                     [NEW] — process-singleton wiring (manual DI; no Hilt)
│
├── ui/                                         [EXTEND]
│   ├── SetupActivity.kt                        [UNCHANGED — still the launcher]
│   ├── MainActivity.kt                         [NEW] — single Activity hosting the bottom-nav NavHost
│   ├── design/                                 [NEW — cohesive reusable component library]
│   │   ├── StatePill.kt
│   │   ├── MetricCard.kt
│   │   ├── InfoRow.kt
│   │   ├── SectionHeader.kt
│   │   ├── EmptyState.kt
│   │   ├── LoadingCard.kt
│   │   ├── Skeleton.kt
│   │   ├── ConnectionBadge.kt
│   │   ├── AnimatedConnectionDot.kt
│   │   ├── Timeline.kt
│   │   ├── ListSection.kt
│   │   └── SenseTopBar.kt
│   ├── nav/
│   │   ├── Destination.kt                      [NEW] — sealed class; compile-time-safe routes
│   │   ├── NavType.kt                          [NEW] — type-safe nav-arg serializers (SessionId, etc.)
│   │   ├── AppNavigation.kt                    [NEW] — NavHost + bottom nav; uses Destination
│   │   └── BottomBar.kt                        [NEW] — 4-destination bottom bar
│   ├── home/
│   │   ├── HomeScreen.kt                       [NEW]
│   │   ├── HomeViewModel.kt                    [NEW] — collects DashboardRepository
│   │   └── HomeUiState.kt                      [NEW] — sealed
│   ├── recordings/
│   │   ├── RecordingsScreen.kt                 [NEW] — list of SessionSummary (paged, lazy)
│   │   ├── RecordingsViewModel.kt              [NEW] — SessionRepository.observeSessions() + loadMore
│   │   ├── RecordingsUiState.kt                [NEW] — sealed (Loading, Empty, Loaded, Error)
│   │   ├── SessionDetailScreen.kt              [NEW] — SessionDetails + TranscriptChunk timeline
│   │   ├── SessionDetailViewModel.kt           [NEW]
│   │   └── SessionDetailUiState.kt             [NEW] — sealed
│   ├── device/
│   │   ├── DeviceScreen.kt                     [NEW] — RelayConnectionState + ServerState detail
│   │   ├── DeviceViewModel.kt                  [NEW] — RelayController + StatusRepository
│   │   └── DeviceUiState.kt                    [NEW] — sealed
│   └── settings/
│       ├── SettingsScreen.kt                   [NEW] — server URL, token, device id, re-provision
│       ├── SettingsViewModel.kt                [NEW] — ConfigurationRepository
│       └── SettingsUiState.kt                  [NEW] — sealed
└── RelayService.kt                             [EXTEND] — see relay/
```

**Why a `core/` package and not a Gradle module:** this slice is one app module.
`core/` is a package-level separation today. When (and only when) a feature needs
isolation, we can lift it into a module. The boundary exists in the type system
(`core/` does not import from `ui/`, `data/`, or `relay/`) so the future lift is
mechanical, not architectural.

**No new Gradle modules in this slice.** The boundary cost is paid in package
discipline, not in Gradle configuration. Keeps the build simple.

**No DI framework.** Hilt/Koin add a configuration tax and codegen for a project
of this size. We use a process-singleton `RepositoryModule` (top-level `object`)
constructed in `Application.onCreate`. Constructors take their dependencies; tests
pass fakes. This matches the existing `StoreHolder` / `RealServerApi` pattern.

## Dependency graph

```
ui.* (Composables)
   ↓
ui.*.ViewModel (collects Repository.stateIn(viewModelScope))
   ↓
data.* (Repository — single source of truth per concern)
   ↓                ↘
http.SenseHttpClient    relay.RelayController (singleton StateFlow)
   ↓                          ↑
core (no project imports)   RelayService (only writer to RelayController)
```

- `ui` depends on `data`, `relay` (read-only StateFlow), `domain`, `core.ui`
- `data` depends on `http`, `domain`, `core`
- `http` depends on `core`
- `relay` depends on `core`; `RelayService` depends on `relay.RelayController` and the
  existing `SensorLink`/`ServerSocket`/`RelaySession`
- `domain` depends only on `core`
- `core` depends on no project packages

No arrows back up. No cross-cutting `*` imports. `LSP` violations are a smell,
not a style choice.

## State ownership (one source of truth per concern)

| Concern               | Owner                            | UI access                                |
|-----------------------|----------------------------------|------------------------------------------|
| App configuration     | `ConfigurationRepository`        | `Flow<Config>`; settings writes through it |
| Relay connection      | `RelayController` (in-process)   | `StateFlow<RelayState>` (read-only)      |
| Server status         | `StatusRepository`               | `observeStatus(): Flow<ServerStatus>`    |
| Device summary        | `DeviceRepository` (derived)     | `StateFlow<DeviceSummary>` from controller |
| Sessions list         | `SessionRepository`              | `observeSessions(): Flow<PagedResult<…>>`|
| Session details       | `SessionRepository`              | `observeSession(id): Flow<Outcome<…>>`   |
| Home dashboard        | `DashboardRepository`            | `StateFlow<DashboardState>`              |

**No state is duplicated across ViewModels.** Every ViewModel collects from one
owner and renders. State updates flow one direction; the next refresh is a
subscription change, not a ViewModel-to-ViewModel bus.

## Repository architecture

Each repository is an `interface` + a `class Impl(...)`. The interface lives in
`data/`, the impl next to it. `RepositoryModule` constructs the concrete impls
once (per process) and is the only place concrete types are named. ViewModels
depend on the interface — host tests pass fakes.

```kotlin
// data/SessionRepository.kt
interface SessionRepository {
    fun observeSessions(): Flow<PagedResult<SessionSummary>>
    suspend fun loadMoreSessions()
    fun observeSession(id: SessionId): Flow<Outcome<SessionDetails>>
    fun observeSessionEvents(id: SessionId): Flow<Outcome<List<CaptureEvent>>>
}
```

`StatusRepository` is the canonical example of "observable, not polled" — its
API is a Flow; the implementation may today `flow { while (true) { … } }` and
tomorrow become a server-sent-events source. UI does not care.

Repositories cache in-memory via `MutableStateFlow` + `stateIn(scope, …)`. The
slice does not add a disk cache; it does not need one. The seam is in place.

## Navigation architecture (typed)

```kotlin
// ui/nav/Destination.kt
sealed interface Destination {
    val route: String
    data object Home : Destination { override val route = "home" }
    data object Recordings : Destination { override val route = "recordings" }
    data object Device : Destination { override val route = "device" }
    data object Settings : Destination { override val route = "settings" }
    data class SessionDetail(val id: SessionId) : Destination {
        override val route = "session/${id.value}"
    }
    data object Setup : Destination { override val route = "setup" }   // gateway back to SetupActivity
}
```

`NavType.SessionIdNavType` provides a type-safe `NavType<SessionId>`. `NavHost`
takes `(Destination) -> Unit` callbacks. `composable<Destination.SessionDetail>`
(Nav Compose 2.8 typed destinations) — the brief asks for compile-time safety
and the framework supports it cleanly.

The Live tab is **deferred** per the brainstorm decision; bottom bar has 4
destinations (Home, Recordings, Device, Settings).

## Data flow (UI → Repository → Network and back)

**Pull (read):**
1. Composable renders.
2. `val state by vm.state.collectAsStateWithLifecycle()`.
3. ViewModel `state = repository.observeX().stateIn(viewModelScope, …)`.
4. Repository's Flow eventually resolves to `Outcome<T>` / `PagedResult<T>` /
   domain object. UI renders each sealed branch.

**Push (user action):**
1. Composable calls a `vm.onX(...)` lambda.
2. ViewModel `viewModelScope.launch { repository.doX(...) }`.
3. Repository performs the action, updates its in-memory state Flow.
4. Observers (this ViewModel + any others) see the new state automatically.

**Server push (future):** When `StatusRepository` becomes an SSE/WebSocket
source, the same Flow contract holds; nothing upstream changes.

## Domain model structure

Sessions are first-class. Each session aggregates per-session capture events
ordered by `start_ms`/`seq`. The CaptureEvent sealed type leaves headroom for
kinds we don't implement yet:

```kotlin
sealed interface CaptureEvent {
    val id: String
    val sessionId: SessionId
    val seq: Int
    val startMs: Long
    val createdAt: Instant
}

data class TranscriptChunk(
    override val id: String,
    override val sessionId: SessionId,
    override val seq: Int,
    override val startMs: Long,
    override val createdAt: Instant,
    val text: String,
    val durationMs: Int,
) : CaptureEvent

data class AudioSegment(...) : CaptureEvent   // placeholder for the audio-plane we don't have yet
data class ImageCapture(...) : CaptureEvent   // placeholder for the future camera
data class MemoryAtomRef(...) : CaptureEvent  // placeholder for the AI memory plane
```

`SessionSummary` is the list-card projection (`id`, `startedAt`, `durationMs`,
`transcriptCount`, `preview`). `SessionDetails` is the full session
(`summary` + the list of `CaptureEvent`s so far). The repository's paged list
view of summaries and the detail view are deliberately separate shapes; future
metadata joins `SessionDetails` without changing the list shape.

## Design system (expanded, cohesive)

`core/ui/` defines **tokens** (colors, type, shape, dimens). `ui/design/`
defines **components** that compose tokens. The component library is opinionated
but minimal — only components that earn their place across at least two screens
in this slice:

| Component                  | Purpose                                           | Used by          |
|----------------------------|---------------------------------------------------|------------------|
| `SenseTopBar`              | Branded top bar with optional back action         | All screens      |
| `SectionHeader`            | H6 with optional trailing action                  | Home, Settings   |
| `StatePill`                | Small monochrome status chip ("Live", "Idle")     | Home, Device     |
| `MetricCard`               | Title + big number + subtitle                     | Home, Session    |
| `InfoRow`                  | Label / value pair, monospace value               | Device, Settings |
| `ListSection`              | Header + lazy list, shared empty/loading states   | Recordings       |
| `EmptyState`               | Icon, title, body, optional CTA                   | Everywhere       |
| `LoadingCard`              | Shimmer placeholder for a card slot               | Home, Recordings |
| `ConnectionBadge`          | Pill combining connection state + reason          | Home, Device     |
| `AnimatedConnectionDot`    | Subtle pulsing dot during Live/Connecting         | Home, Device     |
| `Timeline`                 | Vertical timeline of CaptureEvents                | SessionDetail    |

Components take **domain state**, not raw DTOs or HTTP types. They live above
the repository, never import from `http/` or `data/`.

Theme: a monochrome Material 3 scheme. Single accent is reserved for the live
recording state (a warm graphite-on-ink, not red, to honor "monochrome"). Dark
and light variants. Dynamic color **off** (deliberate — the brand is the
monochrome). `SenseTheme` wraps the existing `MaterialTheme`.

## Server endpoints (new this slice)

All auth-gated (existing `bearer_auth_middleware`). All on the existing
`--http-port`. Response shapes are stable JSON.

- `GET /sessions?limit=N&before=ISO&cursor=…`
  → `{sessions: [{id, started_at, ended_at?, duration_ms, transcript_count, preview}], next_cursor: …|null}`
- `GET /sessions/{id}`
  → `{summary: {...}, events: [{event_id, seq, kind, start_ms, created_at, text?, duration_ms?}]}`
- `GET /status`
  → `{server: {reachable: true, version, uptime_s, active_sessions, total_sessions, recent_events_24h}, device: {address, name?, last_seen?}|null}`

Initial implementation queries the existing `SqliteEventStore` and a new
`SessionIndex` (derived in-memory from events for now; a real index is a
follow-up). The `device` block is populated from the most recent Hello/Bye
record; this slice is server-side state only — the phone's local view of the
device is on the phone (see `DeviceRepository`).

Server-side `tests/http/test_sessions.py` + `test_status.py` follow the existing
`TestClient` + `aiohttp` pattern. `tests/protocol/test_messages.py` and existing
event-store tests are reused as fixtures.

## Why each refinement improves scalability / maintainability

1. **Repository layer.** Networking decisions (HTTP vs WS vs SSE vs local DB)
   become local. ViewModels become testable in isolation with a hand-rolled
   fake. Tomorrow's offline support is a repository implementation, not a
   ViewModel refactor.
2. **Read-only RelayController.** Impossible to accidentally desync relay
   state from a UI surface. One writer, many readers — the same property the
   existing `StoreHolder` enforces for DataStore.
3. **DashboardRepository.** Home is a derived view. If "recent activity" moves
   from HTTP to SSE, only one class changes. The Home screen is the
   "view-model-of-view-models" without the cost.
4. **Separated domain models.** The `CaptureEvent` sealed type is the single
   place new kinds (vision, memory atoms) enter. The list shape is stable; the
   detail view grows. Today the list looks like transcripts; tomorrow it can
   look like a mix without changing the Recordings screen list logic.
5. **Typed navigation.** Refactor-safe. A typo in a route is a compile error.
   The framework (Nav Compose 2.8 typed destinations) supports it natively.
6. **Separate state domains.** Each domain is independently testable. The
   RelayState data class is *composition*, not *megaswitch*. A bug in BLE
   discovery doesn't touch the ServerState branch.
7. **ConfigurationRepository.** Today, four sites read `Config`; today, two
   sites write. When "user preferences" grows, the writes stay where the
   settings go and the reads stay where the data is used.
8. **Pagination-ready.** The list view is a `LazyColumn` over a
   `PagedResult<T>` from day one. When the server returns 5,000 sessions, the
   list doesn't change shape — only the load-more trigger.
9. **Observable status.** The repository *interface* is a Flow. The
   implementation may change. The UI never re-wires.
10. **Expanded design system.** Components are scoped to their concerns (a
    `StatePill` knows about states, not about Home). One change in
    `MetricCard` updates every caller. The component library grows only when
    a second caller needs it.
11. **One source of truth.** A bug surface rule: if the same data appears in
    two ViewModels, the bug is doubled. Repositories are the singular owner;
    ViewModels are pure render-and-dispatch.
12. **Shared core.** Generic helpers (time formatting, Flow combinators, theme
    tokens) are not duplicated. Feature-specific logic stays in feature
    packages, so the core never bloats with feature code.

## Trade-offs introduced

- **More files, more types up front.** A `SessionId` value class and a
  `Destination` sealed class are not strictly necessary for the screens in this
  slice. They are paid for by future refactor safety.
- **No Gradle module split today.** Boundaries are package-level; an IDE refactor
  that violates the package rules is not prevented by the build. We accept this
  in exchange for simpler configuration. The package rule is enforced by
  code review (this slice's PR includes a one-line check in `CLAUDE.md` or
  `AGENTS.md`: "imports must flow downward in the dependency graph").
- **No DI framework.** Manual wiring in `RepositoryModule` works at this scale.
  When a feature needs to be conditionally swapped at runtime (e.g. A/B a new
  SessionRepository), we lift to Hilt. Today the wiring is one file, one
  process-singleton, and the cost is zero.
- **In-memory caching only.** Repositories cache in `MutableStateFlow`s scoped
  to the process. A configuration change recreates the ViewModel; the Flow
  re-subscribes and the cache is repopulated. Disk caching is intentionally
  deferred — there is no offline mode in this slice.
- **No SSE / WebSocket on the read path yet.** `StatusRepository.observeStatus()`
  polls today. The Flow contract is the seam.

## Phase 2 — Design review deliverables

This section is the response to the brief's "Before Writing Significant Code"
checklist, restated as outcomes of the architecture above.

1. **Existing project analysis** — see "Context & decisions" + Phase 1 audit.
2. **Existing architecture** — `2026-07-02-android-setup-pairing-design.md`;
   this slice extends it without modification.
3. **Existing APIs** — `/health`, `/provisioning/pubkey` (auth-gated), WS
   gateway. New endpoints listed above.
4. **Existing UI** — `SetupActivity`/`SetupScreen` (wizard); this slice keeps
   it as the launch destination and adds `MainActivity` for the post-setup
   experience.
5. **Current shortcomings** — no post-setup UI; no visibility into sessions or
   server state; relay service state is private.
6. **Proposed navigation** — `Destination` sealed class; 4 bottom-bar
   destinations; `SessionDetail` pushed from Recordings.
7. **Screen hierarchy** — Home (dashboard), Recordings (paged list) →
   SessionDetail (timeline), Device (relay/server state), Settings (config).
8. **Component hierarchy** — Tokens → Design components → Screens. Each screen
   composes only from `ui/design/` and `core/ui/`.
9. **Data flow** — see "Data flow" section.
10. **API integration strategy** — add the four endpoints; add
    `SenseHttpClient` methods; add DTOs + mappers; add
    `SessionRepository`/`StatusRepository`/etc.
11. **UI design language** — monochrome Material 3, generous spacing, single
    accent reserved for the live state, monochrome icons, large touch targets,
    light + dark.
12. **UX rationale** — the user should glance at Home and know: is the device
    connected, is the server reachable, is there a session going, when did the
    last one end. Recordings is a calm list; tapping one shows the transcript.
    Device is for diagnosis; Settings is for changes. Each screen has a single
    job.
13. **Implementation roadmap** — see Implementation Plan (next document,
    produced by the writing-plans skill).

## Verification plan (this slice's tests)

- **Host unit tests (JVM):** each ViewModel with a fake repository; each
  repository with a fake `SenseHttpClient`; `RelayController` state transitions;
  mappers (DTO ↔ domain); `Destination`/`NavType` round-trips.
- **Existing tests must remain green:** `RelaySessionTest`,
  `SetupViewModelTest`, `ServerConfigTest`, `ProvisioningClientTest`.
- **Server tests:** `tests/http/test_sessions.py`, `tests/http/test_status.py`,
  using the existing `TestClient` fixture. `tests/events/` covers
  `SessionIndex` derivation.
- **No on-device BLE validation in this slice** (the existing bench-side gap;
  unchanged). The UI does not need BLE to be tested in isolation — it observes
  the `RelayController` StateFlow, which can be set directly in a host test.
- **Manual smoke:** build + install + run the setup wizard → MainActivity,
  confirm Home renders from a clean DataStore, confirm Settings edits persist,
  confirm a recording session appears in Recordings within one poll cycle.

## Out of scope (this slice)

- **Live tab.** Deferred per brainstorm. The destination type is not in this
  slice's sealed hierarchy.
- **AI memory / atom display.** The data model is shaped for it; the UI is not.
- **Video retrieval / WiFi settings.** Memory note `transport-ble-vs-wifi` makes
  this a separate, later WiFi-based path. `Settings` exposes a clean extension
  point (a `SectionHeader("Networking")` that today shows only BLE-related rows).
- **Multi-device support.** Single Sense wearable per phone. Unchanged.
- **Push notifications.** Today the only notifiable state is relay; the
  existing foreground service notification suffices.

## Resolved decisions (post-review)

1. **Status refresh cadence.** `StatusRepository.observeStatus()` is a Flow
   the UI never queries directly. Initial implementation polls `GET /status`
   every **2 seconds** while the application is in the foreground, suspends
   polling while backgrounded, and resumes on return. On subscribe, the
   repository immediately emits the most recent cached value (if any), then
   the next poll. The repository's interface is a Flow so that polling can be
   swapped for WebSockets or Server-Sent Events without changing ViewModels
   or UI. The abstraction is `PollingStatusRepository` (impl) implementing
   `StatusRepository` (interface) — the swap is one wiring line in
   `RepositoryModule`.
2. **Re-provisioning from Settings.** Settings exposes a "Reconfigure device"
   action. It launches the existing `SetupActivity` (re-used, not duplicated).
   On success, `SetupActivity` returns to `MainActivity`. On its return, all
   repositories re-read from their sources and the relay state is refreshed
   via `RelayController.requestRefresh()` (the service re-binds, the
   `MutableStateFlow` re-emits current state). No provisioning logic is
   re-implemented; the `SetupViewModel` is the single owner.
3. **Session detail progressive loading.** `SessionDetail` renders the
   `SessionSummary` immediately and shows the events timeline as a
   `LazyColumn` over `SessionRepository.observeSessionEvents(id): Flow<Outcome<List<CaptureEvent>>>`.
   The first emission paints the empty timeline; subsequent emissions append
   chunks without blocking. The ViewModel exposes a sealed `SessionDetailUiState`
   (Loading, LoadedSummary, LoadedSummaryAndEvents(summary, events), Failed).
   The same screen supports a future live-session use case because the
   `Flow` is the source of truth — when a session is in progress, the
   repository's tail of the flow continues to deliver new events as the
   server emits them; the UI does not need to know whether the session is
   "live" or "historical."

## Navigation (confirmed)

Four primary destinations in the bottom bar: **Home, Recordings, Device,
Settings**. The Live tab is intentionally deferred. The `Destination` sealed
class is designed so a fifth entry can be added with a one-line change in
`Destination`, a one-line `composable<Destination.X>` in `AppNavigation`, and
a one-line `NavigationBarItem` in `BottomBar`. No refactor.

---

**Status:** Approved as implementation baseline. Next step: the
`writing-plans` skill produces the implementation plan.
