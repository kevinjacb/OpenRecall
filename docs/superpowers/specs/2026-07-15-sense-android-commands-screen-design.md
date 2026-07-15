# Sense — Android CommandsScreen Composable Design

> **Scope:** This spec covers the single missing user-facing surface for P2-commands: the Android `CommandsScreen` Composable. Everything else in P2-commands (server types, `CommandValidator`, `CommandGuardrails`, idempotency dedup, lifecycle state machine, `SqliteCommandStore`, `/commands` HTTP routes, Android `CommandApi` + `CommandRepository` + `CommandsViewModel`) is already shipped on `main` and out of scope here.

**Goal:** Close the P2-commands loop end-to-end on the user-facing side. The data layer is fully built, tested, and wired (211+ Android tests green, 577 server tests green). The only piece left between the user and the autonomous command path is a Compose screen that lets them *see* the lifecycle.

**Status going in (already on `main`, working):**
- **Server:** `SqliteCommandStore`, dispatcher lifecycle (PENDING → VALIDATED → ISSUED → DELIVERED → EXECUTING → COMPLETED / FAILED / CANCELLED / TIMED_OUT), idempotency dedup, `POST /agent` (IssueCommand path), `GET /commands`, `GET /commands/{id}`, `POST /commands/{id}/ack`. Planner's `IssueCommand` branch live. All 11 P2-commands phases shipped.
- **Android:** `CommandApi` (HTTP wrapper), `Command` / `CommandStatus` / `CommandStatusTransition` domain types, `CommandRecordDto` / `CommandHistoryEntryDto` wire DTOs (snake_case, server wire format), `CommandRepository` (only place that imports `HttpApiError` — INV-11), `CommandsViewModel` (sealed `UiState`: Loading / Ready / Error; `refresh()`, `ack(commandId)` with optimistic state transition), `StubCommandApi` + 8 unit tests in `CommandsViewModelTest.kt`. 211+ Android tests green.
- **What's missing:** `CommandsScreen` Composable — the visible lifecycle list with status badges, the Ack button, and the navigation entry that makes the surface reachable.

**Tech stack:** Kotlin 1.9+ / Jetpack Compose / Hilt / `androidx.lifecycle:lifecycle-runtime-compose` (already on the classpath). No new dependencies.

---

## 1. Architecture

The change is **Android-only**. The data layer is not modified except for one new polling helper added to `CommandsViewModel`. No server changes, no firmware changes, no protocol changes, no new dependencies.

```
┌──────────────────────────────────────────────────────────────┐
│ CommandsScreen (Composable)                                  │
│   ├── collectAsState()  ◄── UiState (Loading | Error | Ready)│
│   ├── LifecycleResumeEffect ─► viewModel.startPolling()      │
│   │                                  │                       │
│   │   onPauseOrDispose ──────────────┼─► viewModel.stopPolling()│
│   │                                  ▼                       │
│   └── renders:                       viewModel.refresh() ───►│
│         LoadingPlaceholder           CommandRepository ────► │
│         ErrorState(retry)                  CommandApi ──►   │
│         ReadyContent                          GET /commands │
│           ├── In flight (n) section                          │
│           └── Done (most recent failure) section             │
└──────────────────────────────────────────────────────────────┘
```

**Layering (binding, matches the project's existing discipline):**
- The Composable holds **no business state**. It collects from the ViewModel and routes user input back to the ViewModel.
- The ViewModel owns the polling lifecycle (the timer job lives on `viewModelScope`).
- The Repository (untouched) is the only place that imports `HttpApiError` (INV-11).
- The Composable imports only the domain `Command` / `CommandStatus` from `data/` and the ViewModel. It does **not** import anything from `http/` or `http/dto/` — a new architectural invariant test guards this.

---

## 2. File layout

### New files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/
  CommandsScreen.kt         — top-level Composable + state branches
  CommandRow.kt             — single-row composable for one command
  CommandStatusPill.kt      — status badge composable

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/util/
  RelativeTime.kt           — small helper: Instant -> "2s ago" / "1m ago" / "3h ago"

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/
  CommandsScreenTest.kt     — Compose UI test (Loading / Error / Ready branches,
                              section grouping, Ack visibility & click, empty state)
  CommandRowTest.kt         — Compose UI test (row rendering for each status / type)
  CommandStatusPillTest.kt  — Compose UI test (one assertion per CommandStatus value)
  RelativeTimeTest.kt       — unit test (boundary cases: 0s, 1s, 60s, 1h, 24h+)

android/sense-relay/app/src/test/kotlin/com/sense/relay/data/
  CommandsViewModelPollingTest.kt  — unit test for the polling helper
```

### Edited files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/
  CommandsViewModel.kt      — add startPolling() / stopPolling() helpers
                              (the existing refresh() / ack() are unchanged and
                              continue to be the only public mutation surface)

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/
  NavDestinations.kt        — add "commands" route constant + bottom-nav entry
  MainNavGraph.kt           — wire the new composable destination

android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/
  ArchitecturalInvariantsTest.kt   — new test: ui/ package must not import
                                     com.sense.relay.http.* or .http.dto.*
                                     (the matching guard for INV-11 on the
                                     UI side; sits alongside the existing
                                     repository-side guard)
```

### Files explicitly NOT changed

- `CommandApi.kt`, `CommandRepository.kt`, all DTOs in `http/dto/` — INV-11 enforced.
- Server-side code (no Phase 12 needed).
- Firmware (no change).
- `build.gradle` / `build.gradle.kts` (no new dependencies).
- Existing screens, navigation, or design tokens.

---

## 3. `CommandsScreen` — top-level Composable

Single top-level Composable that branches on `UiState`. Polling is lifecycle-bound. No business state is held in the Composable.

```kotlin
@Composable
fun CommandsScreen(
    viewModel: CommandsViewModel = hiltViewModel(),
    lifecycleOwner: LifecycleOwner = LocalLifecycleOwner.current,
) {
    val state by viewModel.uiState.collectAsState()

    LifecycleResumeEffect(lifecycleOwner) {
        viewModel.startPolling()
        onPauseOrDispose { viewModel.stopPolling() }
    }

    when (val s = state) {
        is CommandsViewModel.UiState.Loading -> LoadingPlaceholder()
        is CommandsViewModel.UiState.Error   -> ErrorState(
            message = s.message,
            onRetry = viewModel::refresh,
        )
        is CommandsViewModel.UiState.Ready   -> ReadyContent(
            inFlight = s.commands.filter { it.status.isInFlight() },
            done     = s.commands.firstOrNull { it.status.isTerminalFailure() },
        )
    }
}
```

**Branches:**

| `UiState`        | Rendered                                              |
|------------------|-------------------------------------------------------|
| `Loading`        | Full-screen `CircularProgressIndicator` (project std) |
| `Error(msg)`     | Project-standard `ErrorState(msg, onRetry = refresh)` |
| `Ready` empty    | Empty state in the In flight section (no In flight or Done sections) |
| `Ready` with rows| Two sections: In flight (count chip + rows), Done (≤ 1 row, only if a recent failure exists) |

**Status bucketing helpers** (added to a new `ui/util/CommandStatusBuckets.kt`, used by the screen; both helpers use exhaustive `when` over the sealed enum):

```kotlin
fun CommandStatus.isInFlight(): Boolean = when (this) {
    CommandStatus.PENDING, CommandStatus.VALIDATED, CommandStatus.ISSUED,
    CommandStatus.DELIVERED, CommandStatus.EXECUTING -> true
    CommandStatus.COMPLETED, CommandStatus.FAILED,
    CommandStatus.CANCELLED, CommandStatus.TIMED_OUT -> false
}

fun CommandStatus.isTerminalFailure(): Boolean = when (this) {
    CommandStatus.FAILED, CommandStatus.CANCELLED, CommandStatus.TIMED_OUT -> true
    CommandStatus.PENDING, CommandStatus.VALIDATED, CommandStatus.ISSUED,
    CommandStatus.DELIVERED, CommandStatus.EXECUTING, CommandStatus.COMPLETED -> false
}
```

The exhaustive `when` ensures a future new status (e.g. a new `REJECTED` state added in a later phase) is a compile error here, which is the right failure mode — the spec for that future phase will update these helpers as part of its contract.

**Layout (per the project's [[android-ui-style]] — modern, simple, monochrome, super-fluid):**

```
┌──────────────────────────────────────────────┐
│  Commands                          (header)  │
│                                              │
│  In flight                            (2)    │   ← count chip
│  ┌────────────────────────────────────────┐  │
│  │  capture_photo                  [Ack]  │  │   ← only on PENDING
│  │  DELIVERED · 2s ago                    │  │   ← status pill + relative time
│  └────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────┐  │
│  │  record_video                          │  │
│  │  EXECUTING · 8s ago                    │  │
│  └────────────────────────────────────────┘  │
│                                              │
│  Done                                        │   ← only when done != null
│  ┌────────────────────────────────────────┐  │
│  │  request_buffer                        │  │
│  │  FAILED · 1m ago · relay disconnected  │  │   ← reason line on failures
│  └────────────────────────────────────────┘  │
│                                              │
└──────────────────────────────────────────────┘

Empty state (Ready, empty list):

┌──────────────────────────────────────────────┐
│  Commands                                    │
│                                              │
│  No commands yet — try saying                │
│  "take a photo" to the agent.                │
│                                              │
└──────────────────────────────────────────────┘
```

---

## 4. `CommandRow` — single-row composable

One row per command. Inputs are the `Command` domain object. Pure presentation; no state, no callbacks except `onAck: (commandId) -> Unit`.

```kotlin
@Composable
fun CommandRow(
    command: Command,
    now: Instant,                       // injected for testability
    onAck: (commandId: String) -> Unit,
)
```

**Contents:**

- **Title** — `command.type` rendered as a single-line label (one of: `capture_photo`, `record_video`, `request_buffer`, `start_audio`, `stop_audio` — the five command types the project's `IssueCommandPayload` allows; see `IssueCommandPayload` in the existing server code).
- **Subtitle** — `CommandStatusPill` + " · " + relative timestamp via `RelativeTime`.
- **Ack button** — rendered only when `command.status == PENDING`. Tap fires `onAck(command.id)`. Disabled while a refresh is in flight? **No** — optimistic update is the ViewModel's job; the button stays tappable.
- **Reason line** — for FAILED / CANCELLED / TIMED_OUT rows, a third line under the subtitle showing the most recent `CommandHistoryEntryDto.detail.reason` (a short string). On other statuses, no third line.

**Touch targets:** Ack button ≥ 48dp; row height ≥ 64dp.

---

## 5. `CommandStatusPill` — status badge

Small rounded-rectangle badge showing the status text in caps. Pure presentation; no state.

```kotlin
@Composable
fun CommandStatusPill(status: CommandStatus)
```

**Behavior:**
- Renders the status enum name in caps: `PENDING`, `VALIDATED`, `ISSUED`, `DELIVERED`, `EXECUTING`, `COMPLETED`, `FAILED`, `CANCELLED`, `TIMED_OUT`.
- **Monochrome** per the project's UI direction. Single accent for emphasis. **No per-status colors** (no red for FAILED, no green for COMPLETED). The status text is the signal.
- One Compose `Box` + `Text` + `background(MaterialTheme.colorScheme.surfaceVariant, RoundedCornerShape(50))`. No custom drawing.

**Why monochrome:** Stated in [[android-ui-style]] — "monochrome (avoid many color schemes)". Per-status color coding is a YAGNI feature for a screen that lists at most a handful of rows.

---

## 6. `RelativeTime` — small helper

```kotlin
fun relativeTime(then: Instant, now: Instant): String
```

A pure function. Test cases: 0s → "just now", 1s → "1s ago", 59s → "59s ago", 60s → "1m ago", 60m → "1h ago", 24h → "1d ago", 48h → "2d ago", > 30d → ISO date `2026-06-15`. Negative differences clamp to "just now". No localization in this change (English only, matching the rest of the app).

---

## 7. `CommandsViewModel` — polling helper

Two new public methods. The existing `refresh()` and `ack(commandId)` are unchanged and remain the only state-mutation surface.

```kotlin
class CommandsViewModel @Inject constructor(
    private val repo: CommandRepository,
    // ... existing dependencies
) : ViewModel() {

    // existing: val uiState: StateFlow<UiState>
    // existing: fun refresh()  (suspends, idempotent)
    // existing: fun ack(commandId: String)

    private var pollJob: Job? = null

    fun startPolling(intervalMs: Long = 4_000L) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
            // immediate first call so the screen shows fresh data
            // without waiting one tick
            refresh()
            while (isActive) {
                delay(intervalMs)
                refresh()
            }
        }
    }

    fun stopPolling() {
        pollJob?.cancel()
        pollJob = null
    }

    override fun onCleared() {
        stopPolling()
        super.onCleared()
    }
}
```

**Properties:**

- `startPolling()` is **idempotent** — calling it twice does not stack two concurrent timers; the first call's job is cancelled before a new one starts.
- `stopPolling()` is a no-op if polling is not active.
- The job lives on `viewModelScope` — cancelled automatically on `onCleared()`. The explicit `onCleared` override is a belt-and-suspenders cancel so the test for "stopping releases the timer" can use `viewModel.stopPolling()` directly without depending on `onCleared`.
- `refresh()` failures are **not** fatal to the polling loop. The existing `refresh()` already converts a network / parsing failure into `UiState.Error` rather than rethrowing — this is the contract verified by the existing `CommandsViewModelTest`. The polling loop body only calls `refresh()`, so a graceful error state does not break the timer. If `refresh()` ever does throw (an unexpected exception), `viewModelScope.launch` will cancel the job; this is acceptable failure behavior (the next user navigation re-triggers `startPolling`).

**Interval: 4 seconds.** Reasonable for a screen showing "what is the device doing right now" — not chatty enough to drain the radio, not so slow that the user feels stale. The interval is a parameter (`intervalMs: Long = 4_000L`) so tests can use `50ms` for fast assertions.

---

## 8. Navigation

The new destination is added at the top level of the existing `MainNavGraph`:

- **Route:** `"commands"` (constant in `NavDestinations`).
- **Bottom-nav entry:** A new "Commands" tab. Icon: existing project icon family (no new icon font). Position: **between the existing Device tab and Settings** in the bottom-nav order. The principle: Device already exposes device-adjacent state; Commands is the same "what is the device doing" surface but for in-flight agent requests, so they sit together. The implementer picks the precise insertion index by reading `MainNavGraph.kt` at implementation time. If the existing bottom nav has fewer than 4 tabs today, this becomes the 3rd tab.
- **Tapping the tab while already on it** does **not** trigger a manual refresh. Polling handles the refresh story.
- **No back-stack manipulation** — back from Commands returns to the previous tab (or exits the app from the home tab, matching existing behavior).

**No pull-to-refresh.** The 4s poll is the refresh story; adding a second refresh mechanism is YAGNI for an "active only" list.

**No WebSocket push.** Polling only — matches the architectural decision that this change is Android-only and adds no server-side push.

---

## 9. Error handling

| Failure | Surface |
|---|---|
| `GET /commands` returns network error | `UiState.Error` → `ErrorState` composable with Retry button. Polling continues; a successful next poll self-heals. |
| `GET /commands` returns 5xx | Same as above — `UiState.Error` with the message returned by the existing `HttpApiError` mapping in `CommandRepository`. |
| `GET /commands` returns 200 with malformed body | `CommandApi` is the only parser; existing parsing tests cover this. Failure → `UiState.Error`. |
| `POST /commands/{id}/ack` fails after tap | The optimistic update shows `EXECUTING` immediately; the next poll reflects the server's authoritative state (likely back to `PENDING` or `ISSUED`). The button reappears or stays gone based on that state. No toast or error surface for ack failure — the polling loop is the recovery. |
| App rotated while polling | `viewModelScope` survives; `LifecycleResumeEffect` re-binds. Polling continues. |
| Activity in background | `LifecycleResumeEffect` cancels the timer on `onPause`; restarts on `onResume`. No battery drain when not visible. |
| Server returns an unknown `CommandStatus` | `CommandStatus` is a sealed enum on both sides; the `isInFlight()` / `isTerminalFailure()` helpers use exhaustive `when`. A new status is a compile error here, which is the right failure mode. |
| Server returns a command type not in the five known ones | Filtered out client-side as a defense-in-depth measure (the server's `IssueCommand` already validates; this is a UI safety net). |

---

## 10. Edge cases — explicit list

1. **Empty `Ready`** — no In flight section rows, no Done section. Empty state replaces the body.
2. **In flight = 0, Done != null** — only the Done section is shown, with exactly one row (the most recent failure).
3. **In flight > 0, Done = null** — only the In flight section is shown.
4. **Many in-flight commands** — list is scrollable; no pagination. The expected upper bound on "active" commands is single digits (P2-commands is autonomous but not chatty).
5. **Tap on a row that is not PENDING** — does nothing. No detail screen in this scope. (Detail screen is a future feature; the row is informational, not actionable, for non-PENDING statuses.)
6. **Tap on a PENDING row's title (not the Ack button)** — does nothing. The only action is the Ack button.
7. **Server returns 0 commands and the user just spoke a command** — the screen will show the new command within ~4s of the next poll. This is the user-visible "latency" of the polling design; acceptable for a "what is happening" surface, not a "send and confirm" surface.
8. **`commands` destination navigated to directly** (deep link / process restart) — Composable mounts, ViewModel is created, `startPolling` fires via the `LifecycleResumeEffect`, first `refresh()` happens immediately. No special handling needed.

---

## 11. Testing

### Unit tests (host JVM, fast — no Robolectric)

**`CommandsViewModelPollingTest.kt`** — `runTest` + `TestDispatcher`:

- `startPolling calls refresh immediately, then again every interval` — inject a fake `CommandRepository`, set `intervalMs = 50`, advance the test dispatcher 200ms, assert `refreshCount == 5` (one immediate + 4 ticks).
- `startPolling called twice does not stack timers` — set `intervalMs = 50`, call `startPolling` twice, advance 200ms, assert `refreshCount == 5` (not 10).
- `stopPolling cancels subsequent ticks` — `startPolling`, advance 100ms, `stopPolling`, advance 500ms, assert `refreshCount` did not grow after `stopPolling`.
- `stopPolling on a non-polling ViewModel is a no-op` — call `stopPolling` without `startPolling`, assert no exception.
- `onCleared stops polling` — `startPolling`, advance 100ms, call the protected `onCleared` via a test-only override, advance 500ms, assert no growth.
- `refresh failures do not stop the polling loop` — fake repo that throws on first `refresh`, succeeds on second; advance through one full interval; assert `UiState == Ready`.

**`RelativeTimeTest.kt`** — pure function:

- 0s, 1s, 59s, 60s, 119s, 60m, 90m, 24h, 48h, 30d, > 30d, negative (clamps to "just now").

### Compose UI tests (`createComposeRule`)

**`CommandStatusPillTest.kt`** — one assertion per `CommandStatus` value: renders the expected label in caps. The PENDING / EXECUTING / FAILED trio get the full text assertion; the rest are smoke.

**`CommandRowTest.kt`**:

- Renders the `type` as title.
- Renders the status pill text in the subtitle.
- Renders the relative time in the subtitle.
- Ack button is shown when `status == PENDING`.
- Ack button is **not** shown for VALIDATED, ISSUED, DELIVERED, EXECUTING, COMPLETED, FAILED, CANCELLED, TIMED_OUT.
- Ack button click fires `onAck(command.id)` with the correct id.
- FAILED row renders the reason line; non-failed rows do not.
- Renders all five command types without crashing (one parameterized test).

**`CommandsScreenTest.kt`** — the contract test:

- Renders the loading indicator when `UiState.Loading`.
- Renders `ErrorState` with Retry when `UiState.Error("boom")`.
- Tapping Retry fires the injected `refresh` lambda.
- Renders `Ready` with one in-flight command under "In flight" with count chip "1".
- Renders `Ready` with two in-flight + one failed: shows "In flight (2)" and "Done" sections, Done has exactly one row.
- Renders `Ready` with empty list: shows the empty state, no In flight or Done sections.
- Tapping Ack on a PENDING row fires the injected `ack(commandId)` with the correct id.
- The polling helper is **not** exercised here (covered in `CommandsViewModelPollingTest`); only the visible result of state changes is checked.

### Architectural invariant test

**`ArchitecturalInvariantsTest.kt`** (located at `app/src/test/kotlin/com/sense/relay/arch/ArchitecturalInvariantsTest.kt`):

- Asserts that no file under `ui/` imports `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`. The test is a file-tree scan: read every `.kt` file under `app/src/main/kotlin/com/sense/relay/ui/`, fail if any line matches `import com.sense.relay.http`. This is the UI-side guard for INV-11 (the existing repo-side guard ensures the repository is the only place that imports `HttpApiError`; this test ensures the UI doesn't bypass it).
- The test is a host-JVM unit test, no Android dependencies, runs in milliseconds.

### What this does NOT add

- No instrumentation tests (pure Compose UI; no platform APIs beyond what Compose provides).
- No screenshot / Paparazzi tests.
- No new fakes for the server (the existing `StubCommandApi` and the new polling test fakes are sufficient).
- No visual regression baseline.

---

## 12. Non-goals / deferred

- Detail screen for a tapped command (PENDING or otherwise).
- Pull-to-refresh on top of the polling loop.
- WebSocket push from the server (would require a server change).
- Showing COMPLETED / CANCELLED commands in the In flight or Done list.
- Local search / filter on the list.
- Manual "Refresh" button in the app bar (the 4s poll is the refresh story; the Error state's Retry button is the only explicit refresh).
- Localization of `RelativeTime` (English only).
- Animations on status transitions (the project uses subtle defaults; explicit transitions are a future polish).
- Showing command params (e.g. `record_video.duration_s`) — these are not surfaced in the list; a future detail screen can.

---

## 13. Design principles (binding for the plan)

- **The Composable is a view.** It holds no business state, no filter state, no derived state. It collects, branches, and dispatches.
- **The ViewModel owns the polling lifecycle.** A coroutine job on `viewModelScope`, started and stopped by the screen but owned by the ViewModel.
- **The data layer is unchanged.** `CommandApi`, `CommandRepository`, the DTOs, and the existing `CommandsViewModel.refresh()` / `ack()` API are the contract.
- **Monochrome UI.** No per-status colors. Status text is the signal.
- **Polling is the refresh story.** No pull-to-refresh, no push, no manual button (except Error state's Retry).
- **Active-only scope.** The screen is "what is the device doing right now." Historical inspection is a future Memory / History feature.
- **Existing patterns.** Compose, Hilt, sealed `UiState`, the project's design system, the existing bottom nav, the existing `ErrorState` and loading placeholders. No new dependencies.
- **Architectural invariant preserved.** The Composable imports only domain types from `data/`. INV-11 is reinforced with a new test.
- **TDD throughout.** Unit tests for the polling helper and `RelativeTime`; Compose UI tests for the screen, the row, and the pill. Architectural invariant test for the UI-layer boundary.
- **No rewrites of working code.** Extend, append. The data layer is the contract; the screen is the surface.

---

## 14. Phasing (single phase; this spec is one chunk)

This is **one** Android-only chunk, intentionally not broken into sub-phases. The data layer, the ViewModel, the existing tests, and the existing navigation infrastructure are all in place. The chunk is "add a Composable, two small helper composables, a `RelativeTime` helper, two new ViewModel methods, a nav entry, and a full test suite for the new code." Estimated: small-medium, ships in a single feature branch with `main` staying green at every commit.

**Working style:** contracts first (the `CommandRow` / `CommandStatusPill` / `RelativeTime` / `CommandsViewModel.startPolling` signatures are the contract), then TDD on the polling helper and `RelativeTime`, then the Composable + UI tests, then the nav wiring. `main` stays green at every step.
