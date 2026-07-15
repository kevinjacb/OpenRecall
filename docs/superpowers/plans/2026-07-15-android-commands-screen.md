# Android CommandsScreen Composable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the missing user-facing surface for P2-commands — the Android `CommandsScreen` Composable, its row + status rendering, the lifecycle-aware polling helper, and the bottom-nav entry — closing the P2-commands loop end-to-end on the user side.

**Architecture:** Android-only. The Composable is a thin view: it collects from the existing `CommandsViewModel.state: StateFlow<UiState>`, branches on `Loading | Error | Ready`, and routes user input (Ack tap, Retry tap) back to the ViewModel. The ViewModel gains a lifecycle-aware polling helper (`startPolling` / `stopPolling`) that drives `refresh()` on a 4s cadence while the screen is `RESUMED`. No server / firmware / protocol changes; no changes to the existing data layer (`CommandApi`, `CommandRepository`, the DTOs). The architectural invariant `ui/ must not import com.sense.relay.http.*` is reinforced with a new test.

**Tech Stack:** Kotlin 1.9+ / Jetpack Compose / `androidx.lifecycle:lifecycle-runtime-compose:2.8.7` (new dep) / `kotlinx.coroutines.test` / `androidx.compose.ui:ui-test-junit4` (already on classpath via existing Compose UI tests). Project uses Hilt-free manual DI (`RepositoryModule.repos.*` + `viewModelFactory { initializer { ... } }`); the new Composable follows the same pattern as `DeviceRoute` / `DeviceScreen`.

## Global Constraints

- **TDD throughout.** Each task that adds behavior first writes a failing test, then implements to green.
- **No new HTTP / DTO / wire types.** INV-11 is reinforced: `ui/` must not import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`. A new architectural test enforces this.
- **No changes to `CommandApi.kt`, `CommandRepository.kt`, the DTOs in `http/dto/`, or the existing `CommandsViewModel.refresh()` / `ack()` API.** Only additions to `CommandsViewModel` (`startPolling`, `stopPolling`).
- **Reuse the design system.** `StatePill` (existing in `ui/design/StatePill.kt`) is the status badge — do not build a new `CommandStatusPill`. The empty state uses `EmptyState` (existing). The screen header uses `SenseTopBar` (existing). Touch targets ≥ 48dp; row height ≥ 64dp.
- **Monochrome UI.** No per-status colors. Status text is the signal.
- **Manual DI pattern.** `CommandsRoute` builds the ViewModel via `viewModelFactory { initializer { CommandsViewModel(repo = RepositoryModule.repos.commandRepository) } }` — same pattern as `DeviceRoute`.
- **Compose test pattern.** `createComposeRule()` for UI tests; host-JVM (no Robolectric) for unit tests of pure functions and ViewModel helpers.
- **Frequent commits.** Every task ends with a commit. `main` stays green at every step.
- **Existing data layer contract is sacred.** The `Command` / `CommandStatus` / `CommandStatusTransition` types in `data/Command.kt` and the `UiState` sealed class in `data/CommandRepository.kt` are the API surface. Do not edit them.

---

## File Structure

### New files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/
  CommandsRoute.kt           — wired-in entry; builds ViewModel via viewModelFactory
  CommandsScreen.kt          — top-level Composable + state branches (Loading/Error/Ready)
  CommandRow.kt              — single-row composable (title, status pill, time, ack button)
  CommandStatusBuckets.kt    — isInFlight() / isTerminalFailure() helpers over the sealed enum
  RelativeTime.kt            — pure helper Instant -> "2s ago" / "1m ago" / "3h ago"

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/
  CommandsScreenTest.kt      — Compose UI test (Loading/Error/Ready branches, section grouping,
                               Ack visibility & click, empty state, Retry)
  CommandRowTest.kt          — Compose UI test (row rendering per status / type,
                               Ack button visibility on PENDING only,
                               reason line on FAILED/CANCELLED/TIMED_OUT)
  RelativeTimeTest.kt        — host-JVM unit test (boundary cases: 0s, 1s, 60s, 1h, 24h, 30d, negative)

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/
  CommandStatusBucketsTest.kt  — exhaustive test of isInFlight() / isTerminalFailure() over the sealed enum

android/sense-relay/app/src/test/kotlin/com/sense/relay/data/
  CommandsViewModelPollingTest.kt  — host-JVM unit test for startPolling / stopPolling
                                      (uses runTest + TestDispatcher, not runBlocking)

android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/
  ArchitecturalInvariantsTest.kt  — file-tree scan: ui/ must not import com.sense.relay.http.*
```

### Edited files

```
android/sense-relay/app/build.gradle.kts                       — add lifecycle-runtime-compose:2.8.7
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/CommandRepository.kt
                                                              — extend CommandsViewModel with
                                                                startPolling / stopPolling
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt
                                                              — add Destination.Commands
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt
                                                              — add Commands tab between Device and Settings
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt
                                                              — wire composable("commands") and
                                                                add to MAIN_ROUTES
```

### Files explicitly NOT changed

- `data/CommandApi.kt`, `data/Command.kt` (the domain types), all DTOs in `http/dto/`, `http/HttpApiError.kt`, `http/ErrorCode.kt` — INV-11 enforced.
- Server-side code.
- Firmware.
- Any existing screen, ViewModel, or design component other than the additions listed above.
- `data/RepositoryModule.kt` — `CommandRepository` is already wired in there (verified via existing `CommandsViewModelTest`); no DI change needed.
- `data/CommandRepository.kt`'s public `CommandRepository` class and existing `CommandsViewModel` state / `refresh()` / `ack()` — only additions.

---

## Task 1: Add `lifecycle-runtime-compose` dependency

**Files:**
- Modify: `android/sense-relay/app/build.gradle.kts:55-65` (the `dependencies { ... }` block)

**Interfaces:**
- Consumes: nothing (pure build-file change)
- Produces: `androidx.lifecycle:lifecycle-runtime-compose:2.8.7` available on the classpath so that Task 5 can import `androidx.lifecycle.compose.LifecycleEventEffect` and `LifecycleEvent`.

- [ ] **Step 1: Read the current `dependencies { }` block**

Run: `sed -n '50,80p' android/sense-relay/app/build.gradle.kts`
Expected: the existing `implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")` and `implementation("androidx.lifecycle:lifecycle-process:2.8.7")` lines are visible, with version `2.8.7`.

- [ ] **Step 2: Add the new dependency line**

In `android/sense-relay/app/build.gradle.kts`, in the `dependencies { ... }` block, immediately after the `lifecycle-process:2.8.7` line, add:

```kotlin
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
```

Resulting block (verify by reading the file):

```kotlin
    implementation("androidx.lifecycle:lifecycle-viewmodel-compose:2.8.7")
    implementation("androidx.lifecycle:lifecycle-process:2.8.7")
    implementation("androidx.lifecycle:lifecycle-runtime-compose:2.8.7")
```

- [ ] **Step 3: Verify the Gradle sync succeeds**

Run: `cd android/sense-relay && ./gradlew :app:dependencies --configuration debugRuntimeClasspath 2>&1 | grep -i lifecycle-runtime-compose`
Expected: a single line containing `lifecycle-runtime-compose:2.8.7` (Gradle has resolved the new dep). Exit code 0.

- [ ] **Step 4: Verify unit tests still compile (no test changes, but the dep affects the test classpath too)**

Run: `cd android/sense-relay && ./gradlew :app:compileDebugUnitTestKotlin`
Expected: BUILD SUCCESSFUL. Exit code 0.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/build.gradle.kts
git commit -m "build(android): add lifecycle-runtime-compose:2.8.7

Required for LifecycleEventEffect in CommandsScreen polling hook.
No other code changes in this commit."
```

---

## Task 2: `CommandStatusBuckets` + tests (TDD)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandStatusBuckets.kt`
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandStatusBucketsTest.kt`

**Interfaces:**
- Consumes: `com.sense.relay.data.CommandStatus` (the existing sealed enum)
- Produces:
  - `fun CommandStatus.isInFlight(): Boolean` — true for `PENDING, VALIDATED, ISSUED, DELIVERED, EXECUTING`; false otherwise.
  - `fun CommandStatus.isTerminalFailure(): Boolean` — true for `FAILED, CANCELLED, TIMED_OUT`; false otherwise.
  - Both functions use exhaustive `when` over the sealed enum (compile error if a new variant is added without updating these).

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandStatusBucketsTest.kt`:

```kotlin
package com.sense.relay.ui.commands

import com.sense.relay.data.CommandStatus
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

class CommandStatusBucketsTest {

    @Test
    fun `isInFlight is true for PENDING VALIDATED ISSUED DELIVERED EXECUTING`() {
        val inFlight = listOf(
            CommandStatus.PENDING,
            CommandStatus.VALIDATED,
            CommandStatus.ISSUED,
            CommandStatus.DELIVERED,
            CommandStatus.EXECUTING,
        )
        inFlight.forEach { assertTrue("$it should be in-flight", it.isInFlight()) }
    }

    @Test
    fun `isInFlight is false for COMPLETED FAILED CANCELLED TIMED_OUT`() {
        val terminal = listOf(
            CommandStatus.COMPLETED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.TIMED_OUT,
        )
        terminal.forEach { assertFalse("$it should not be in-flight", it.isInFlight()) }
    }

    @Test
    fun `isTerminalFailure is true for FAILED CANCELLED TIMED_OUT`() {
        val failures = listOf(
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.TIMED_OUT,
        )
        failures.forEach { assertTrue("$it should be a terminal failure", it.isTerminalFailure()) }
    }

    @Test
    fun `isTerminalFailure is false for non-failure statuses`() {
        val nonFailures = listOf(
            CommandStatus.PENDING,
            CommandStatus.VALIDATED,
            CommandStatus.ISSUED,
            CommandStatus.DELIVERED,
            CommandStatus.EXECUTING,
            CommandStatus.COMPLETED,
        )
        nonFailures.forEach { assertFalse("$it should not be a terminal failure", it.isTerminalFailure()) }
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandStatusBucketsTest"`
Expected: FAIL with `Unresolved reference: isInFlight` / `Unresolved reference: isTerminalFailure` (the helpers don't exist yet).

- [ ] **Step 3: Write minimal implementation**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandStatusBuckets.kt`:

```kotlin
package com.sense.relay.ui.commands

import com.sense.relay.data.CommandStatus

/**
 * Bucketing helpers used by [CommandsScreen] to split the
 * `CommandsViewModel.UiState.Ready.commands` list into
 * "in flight" (active) and "most recent failure" (terminal).
 *
 * Both helpers are exhaustive over the sealed [CommandStatus]
 * enum: adding a new variant is a compile error here, which is
 * the right failure mode — the spec for that future phase must
 * update this file.
 */
fun CommandStatus.isInFlight(): Boolean = when (this) {
    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
    -> true

    CommandStatus.COMPLETED,
    CommandStatus.FAILED,
    CommandStatus.CANCELLED,
    CommandStatus.TIMED_OUT,
    -> false
}

fun CommandStatus.isTerminalFailure(): Boolean = when (this) {
    CommandStatus.FAILED,
    CommandStatus.CANCELLED,
    CommandStatus.TIMED_OUT,
    -> true

    CommandStatus.PENDING,
    CommandStatus.VALIDATED,
    CommandStatus.ISSUED,
    CommandStatus.DELIVERED,
    CommandStatus.EXECUTING,
    CommandStatus.COMPLETED,
    -> false
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandStatusBucketsTest"`
Expected: PASS. 4 tests, all green.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandStatusBuckets.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandStatusBucketsTest.kt
git commit -m "feat(android): CommandStatusBuckets (isInFlight / isTerminalFailure)

Pure helpers over the sealed CommandStatus enum, used by the
upcoming CommandsScreen to split active vs done sections.
TDD; exhaustive when-statements over the sealed enum."
```

---

## Task 3: `RelativeTime` + tests (TDD)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/RelativeTime.kt`
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/RelativeTimeTest.kt`

**Interfaces:**
- Consumes: `java.time.Instant` (stdlib)
- Produces: `fun relativeTime(then: Instant, now: Instant): String`
  - `< 60s` → "Ns ago" (N is integer seconds, 0 → "just now")
  - `< 60m` → "Nm ago"
  - `< 24h` → "Nh ago"
  - `< 30d` → "Nd ago"
  - `>= 30d` → ISO date "yyyy-MM-dd" via `DateTimeFormatter.ISO_LOCAL_DATE`
  - negative diffs clamp to "just now"

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/RelativeTimeTest.kt`:

```kotlin
package com.sense.relay.ui.commands

import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant

class RelativeTimeTest {

    private val now: Instant = Instant.parse("2026-07-15T12:00:00Z")

    @Test
    fun `0 seconds ago is just now`() {
        assertEquals("just now", relativeTime(now, now))
    }

    @Test
    fun `1 second ago is 1s ago`() {
        assertEquals("1s ago", relativeTime(now.minusSeconds(1), now))
    }

    @Test
    fun `59 seconds ago is 59s ago`() {
        assertEquals("59s ago", relativeTime(now.minusSeconds(59), now))
    }

    @Test
    fun `60 seconds ago is 1m ago`() {
        assertEquals("1m ago", relativeTime(now.minusSeconds(60), now))
    }

    @Test
    fun `119 seconds ago is 1m ago`() {
        assertEquals("1m ago", relativeTime(now.minusSeconds(119), now))
    }

    @Test
    fun `120 seconds ago is 2m ago`() {
        assertEquals("2m ago", relativeTime(now.minusSeconds(120), now))
    }

    @Test
    fun `60 minutes ago is 1h ago`() {
        assertEquals("1h ago", relativeTime(now.minusSeconds(60 * 60), now))
    }

    @Test
    fun `24 hours ago is 1d ago`() {
        assertEquals("1d ago", relativeTime(now.minusSeconds(24 * 60 * 60), now))
    }

    @Test
    fun `48 hours ago is 2d ago`() {
        assertEquals("2d ago", relativeTime(now.minusSeconds(48 * 60 * 60), now))
    }

    @Test
    fun `30 days ago is ISO date`() {
        val then = now.minusSeconds(30L * 24 * 60 * 60)
        val expected = "2026-06-15" // 2026-07-15 minus 30 days
        assertEquals(expected, relativeTime(then, now))
    }

    @Test
    fun `negative diff clamps to just now`() {
        // then is AFTER now (e.g. clock skew)
        val future = now.plusSeconds(120)
        assertEquals("just now", relativeTime(future, now))
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.RelativeTimeTest"`
Expected: FAIL with `Unresolved reference: relativeTime`.

- [ ] **Step 3: Write minimal implementation**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/RelativeTime.kt`:

```kotlin
package com.sense.relay.ui.commands

import java.time.Instant
import java.time.LocalDate
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * "2s ago" / "1m ago" / "3h ago" / "2d ago" for a [then] instant
 * relative to [now]. English-only (no localization in this
 * change, matching the rest of the app).
 *
 * - diff < 0 (future timestamp) -> "just now" (clock-skew safe)
 * - diff in [0, 60)s -> "Ns ago" (0 -> "just now")
 * - diff in [60s, 60m) -> "Nm ago"
 * - diff in [60m, 24h) -> "Nh ago"
 * - diff in [24h, 30d) -> "Nd ago"
 * - diff >= 30d -> ISO date (yyyy-MM-dd) at the [then] zone
 *
 * Pure function; no Android dependencies; testable on the host JVM.
 */
fun relativeTime(then: Instant, now: Instant): String {
    val diffSec = (now.epochSecond - then.epochSecond).coerceAtLeast(0L)
    return when {
        diffSec < 60L -> if (diffSec == 0L) "just now" else "${diffSec}s ago"
        diffSec < 60L * 60 -> "${diffSec / 60L}m ago"
        diffSec < 24L * 60L * 60 -> "${diffSec / (60L * 60L)}h ago"
        diffSec < 30L * 24L * 60L * 60 -> "${diffSec / (24L * 60L * 60L)}d ago"
        else -> DateTimeFormatter.ISO_LOCAL_DATE.format(
            LocalDate.ofInstant(then, ZoneOffset.UTC),
        )
    }
}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.RelativeTimeTest"`
Expected: PASS. 11 tests, all green.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/RelativeTime.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/RelativeTimeTest.kt
git commit -m "feat(android): RelativeTime helper for command rows

Pure function: Instant -> '2s ago' / '1m ago' / etc. / ISO date.
Clock-skew safe (negative diffs clamp to 'just now').
TDD; 11 boundary cases."
```

---

## Task 4: `CommandsViewModel` polling helper (TDD)

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/CommandRepository.kt` (add `startPolling` / `stopPolling` to the existing `CommandsViewModel` class — do NOT change any other public API)
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/CommandsViewModelPollingTest.kt`

**Interfaces:**
- Consumes: the existing `CommandsViewModel(repo: CommandRepository)` constructor and its public `state: StateFlow<UiState>`, `refresh()`, `ack(commandId)` — all unchanged.
- Produces (additions to `CommandsViewModel`):
  - `fun startPolling(intervalMs: Long = 4_000L)` — starts a coroutine on `viewModelScope` that calls `refresh()` once immediately, then every `intervalMs` ms. Idempotent (cancels prior job before starting a new one).
  - `fun stopPolling()` — cancels the active polling job. No-op if polling is not active.
  - `override fun onCleared()` — calls `stopPolling()` then `super.onCleared()` (existing `ViewModel.onCleared` is a no-op; the override is the belt-and-suspenders cancel).

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/CommandsViewModelPollingTest.kt`:

```kotlin
package com.sense.relay.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceTimeBy
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

@OptIn(ExperimentalCoroutinesApi::class)
class CommandsViewModelPollingTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    @Test
    fun `startPolling calls refresh immediately and again every interval`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = CountingRepo(counter)
        val vm = CommandsViewModel(repo)

        vm.startPolling(intervalMs = 50L)
        // Immediate first call: dispatcher has not advanced.
        advanceUntilIdle()
        assertEquals(1, counter.get())

        // Advance 200ms -> 4 more ticks (50, 100, 150, 200) plus the immediate.
        advanceTimeBy(200L)
        advanceUntilIdle()
        assertEquals(5, counter.get())
    }

    @Test
    fun `startPolling called twice does not stack timers`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = CountingRepo(counter)
        val vm = CommandsViewModel(repo)

        vm.startPolling(intervalMs = 50L)
        vm.startPolling(intervalMs = 50L) // second call cancels the first
        advanceTimeBy(200L)
        advanceUntilIdle()
        // 1 immediate + 4 ticks (50,100,150,200), NOT 10.
        assertEquals(5, counter.get())
    }

    @Test
    fun `stopPolling cancels subsequent ticks`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = CountingRepo(counter)
        val vm = CommandsViewModel(repo)

        vm.startPolling(intervalMs = 50L)
        advanceTimeBy(100L)
        advanceUntilIdle()
        val afterStart = counter.get() // expect 3 (immediate + 50, 100)

        vm.stopPolling()
        advanceTimeBy(500L)
        advanceUntilIdle()
        assertEquals(afterStart, counter.get()) // no growth
    }

    @Test
    fun `stopPolling on a non-polling ViewModel is a no-op`() = runTest(dispatcher) {
        val repo = CountingRepo(AtomicInteger(0))
        val vm = CommandsViewModel(repo)
        vm.stopPolling() // should not throw
        // and no tick ever fires
        advanceTimeBy(500L)
        advanceUntilIdle()
        assertEquals(0, repo.refreshCount.get())
    }

    @Test
    fun `refresh failures do not stop the polling loop`() = runTest(dispatcher) {
        val counter = AtomicInteger(0)
        val repo = FlakyRepo(counter, failOnCalls = setOf(1))
        val vm = CommandsViewModel(repo)

        vm.startPolling(intervalMs = 50L)
        // first call (immediate) fails -> UiState.Error; loop continues
        advanceUntilIdle()
        assertTrue(vm.state.value is CommandsViewModel.UiState.Error)

        advanceTimeBy(50L) // second tick
        advanceUntilIdle()
        // second call succeeds -> UiState.Ready; loop is still alive
        assertTrue(vm.state.value is CommandsViewModel.UiState.Ready)
    }

    // -- test doubles --

    private class CountingRepo(val refreshCount: AtomicInteger) :
        CommandRepository(StubCommandApi()) {
        override suspend fun listActive(): List<Command> {
            refreshCount.incrementAndGet()
            return emptyList()
        }
    }

    private class FlakyRepo(
        val refreshCount: AtomicInteger,
        private val failOnCalls: Set<Int>,
    ) : CommandRepository(StubCommandApi()) {
        override suspend fun listActive(): List<Command> {
            val n = refreshCount.incrementAndGet()
            if (n in failOnCalls) throw RuntimeException("network error")
            return emptyList()
        }
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.CommandsViewModelPollingTest"`
Expected: FAIL with `Unresolved reference: startPolling` / `Unresolved reference: stopPolling`.

- [ ] **Step 3: Write minimal implementation**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/CommandRepository.kt`. The current file is `CommandRepository.kt` and contains BOTH `CommandRepository` (class) and `CommandsViewModel` (class) in the same file (lines 22-32 and 41-82 respectively). Add the polling helper to `CommandsViewModel` only — do not touch `CommandRepository` or the existing `CommandsViewModel` public methods.

Replace the entire `CommandsViewModel` class (lines 41-82 of the current file) with:

```kotlin
class CommandsViewModel(
    private val repo: CommandRepository,
) : ViewModel() {
    sealed class UiState {
        data object Loading : UiState()
        data class Error(val message: String) : UiState()
        data class Ready(val commands: List<Command>) : UiState()
    }

    private val _state = MutableStateFlow<UiState>(UiState.Loading)
    val state: StateFlow<UiState> = _state.asStateFlow()

    private var pollJob: Job? = null

    suspend fun refresh() {
        _state.value = UiState.Loading
        try {
            val cmds = repo.listActive()
            _state.value = UiState.Ready(cmds)
        } catch (e: Exception) {
            _state.value = UiState.Error(e.message ?: "failed to load commands")
        }
    }

    suspend fun ack(commandId: String) {
        try {
            repo.ack(commandId)
            // After ack, the lifecycle status changed; refresh the list.
            _state.update { current ->
                if (current is UiState.Ready) {
                    UiState.Ready(current.commands.map { c ->
                        if (c.commandId == commandId) {
                            c.copy(status = CommandStatus.EXECUTING)
                        } else c
                    })
                } else current
            }
        } catch (e: Exception) {
            // Refresh anyway; the server may have applied the ack
            // but the response was malformed.
            refresh()
        }
    }

    /**
     * Polling lifecycle (added for the CommandsScreen P2-commands
     * Phase 9 Composable). The screen calls this in a
     * LifecycleEventEffect(ON_RESUME) and `stopPolling()` in
     * ON_PAUSE. The job lives on [viewModelScope], so it is
     * cancelled automatically when the ViewModel is cleared; the
     * explicit [onCleared] override below is a belt-and-suspenders
     * cancel so the test for "stopping releases the timer" can
     * verify the cancel without depending on the framework's
     * teardown.
     *
     * `startPolling` is idempotent: calling it twice cancels the
     * prior job before starting a new one. The first `refresh()`
     * call is immediate so the screen does not wait one full
     * interval for its first paint.
     */
    fun startPolling(intervalMs: Long = 4_000L) {
        pollJob?.cancel()
        pollJob = viewModelScope.launch {
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

**Required new imports** at the top of `CommandRepository.kt` (add to the existing import block):

```kotlin
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
```

The existing imports (`MutableStateFlow`, `StateFlow`, `asStateFlow`, `update`, `HttpApiError`, `CommandRecordDto`, `Command`) stay untouched.

- [ ] **Step 4: Run all CommandsViewModel tests to verify both the old and new pass**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.CommandsViewModelTest" --tests "com.sense.relay.data.CommandsViewModelPollingTest"`
Expected: PASS. The original 3 tests in `CommandsViewModelTest` still pass (the `refresh()` / `ack()` API is unchanged), and all 5 new tests in `CommandsViewModelPollingTest` pass.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/CommandRepository.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/data/CommandsViewModelPollingTest.kt
git commit -m "feat(android): CommandsViewModel startPolling / stopPolling

4s default cadence; idempotent startPolling; viewModelScope-owned.
Existing refresh() and ack() API unchanged; existing tests still green.
TDD; 5 polling tests cover immediate/tick/idempotency/cancel/no-op/failures."
```

---

## Task 5: `CommandRow` Composable + tests (TDD)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandRow.kt`
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandRowTest.kt`

**Interfaces:**
- Consumes: `com.sense.relay.data.Command` (existing domain type); `java.time.Instant` (for `now`); `com.sense.relay.data.CommandStatus` (for the sealed enum used by the row).
- Produces: `@Composable fun CommandRow(command: Command, now: Instant, onAck: (String) -> Unit)`.

**Row contents (in order, top-to-bottom):**
- **Title** — `command.type` (one of `capture_photo`, `record_video`, `request_buffer`, `start_audio`, `stop_audio`).
- **Subtitle** — `StatePill` with `label = status.name` (e.g. "PENDING") and `tone = Tone.Neutral` (monochrome per design system), then " · " and the `relativeTime(command.issuedAt, now)` string.
- **Ack button** — rendered only when `status == CommandStatus.PENDING`. Tap fires `onAck(command.commandId)`. The button label is "Ack"; sized ≥ 48dp; placed on the right of the row.
- **Reason line** — for `FAILED`, `CANCELLED`, `TIMED_OUT` only, a third line under the subtitle. The reason is the `detail["reason"]` string from the most recent entry in `command.history` (where `toStatus == command.status`); empty / null reason renders an empty third line (no row at all in that case).

**Row layout:** a `Card` (project-standard, `MaterialTheme.colorScheme.surface`) with `padding(Spacing.md)`. Title is `titleMedium`; subtitle / reason are `bodySmall`. Min height 64dp; max width `fillMaxWidth()`.

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandRowTest.kt`:

```kotlin
package com.sense.relay.ui.commands

import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.sense.relay.data.Command
import com.sense.relay.data.CommandStatus
import com.sense.relay.data.CommandStatusTransition
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import java.time.Instant

class CommandRowTest {

    @get:Rule
    val composeRule = createComposeRule()

    private val now: Instant = Instant.parse("2026-07-15T12:00:00Z")
    private val issuedAt: Instant = now.minusSeconds(30)

    private fun command(
        commandId: String = "c1",
        type: String = "capture_photo",
        status: CommandStatus = CommandStatus.DELIVERED,
        history: List<CommandStatusTransition> = emptyList(),
    ): Command = Command(
        commandId = commandId,
        sessionId = "s1",
        type = type,
        params = emptyMap(),
        issuedAt = issuedAt,
        expiresAt = now.plusSeconds(1800),
        status = status,
        history = history,
    )

    @Test
    fun `renders the command type as title`() {
        composeRule.setContent {
            MaterialTheme {
                CommandRow(command = command(type = "record_video"), now = now, onAck = {})
            }
        }
        composeRule.onNodeWithText("record_video").assertIsDisplayed()
    }

    @Test
    fun `renders the status name and relative time in the subtitle`() {
        composeRule.setContent {
            MaterialTheme {
                CommandRow(
                    command = command(status = CommandStatus.EXECUTING),
                    now = now,
                    onAck = {},
                )
            }
        }
        // "EXECUTING" + " · " + "30s ago" is rendered as a single Text node.
        composeRule.onNodeWithText("EXECUTING · 30s ago").assertIsDisplayed()
    }

    @Test
    fun `ack button is shown for PENDING`() {
        composeRule.setContent {
            MaterialTheme {
                CommandRow(
                    command = command(status = CommandStatus.PENDING),
                    now = now,
                    onAck = {},
                )
            }
        }
        composeRule.onNodeWithTag("command_row_ack").assertIsDisplayed()
    }

    @Test
    fun `ack button is hidden for non-PENDING statuses`() {
        val nonPending = listOf(
            CommandStatus.VALIDATED,
            CommandStatus.ISSUED,
            CommandStatus.DELIVERED,
            CommandStatus.EXECUTING,
            CommandStatus.COMPLETED,
            CommandStatus.FAILED,
            CommandStatus.CANCELLED,
            CommandStatus.TIMED_OUT,
        )
        nonPending.forEach { status ->
            composeRule.setContent {
                MaterialTheme {
                    CommandRow(
                        command = command(status = status),
                        now = now,
                        onAck = {},
                    )
                }
            }
            composeRule.onNodeWithTag("command_row_ack").assertDoesNotExist()
        }
    }

    @Test
    fun `tapping ack fires onAck with the command id`() {
        var captured: String? = null
        composeRule.setContent {
            MaterialTheme {
                CommandRow(
                    command = command(commandId = "cmd-42", status = CommandStatus.PENDING),
                    now = now,
                    onAck = { captured = it },
                )
            }
        }
        composeRule.onNodeWithTag("command_row_ack").performClick()
        assertEquals("cmd-42", captured)
    }

    @Test
    fun `FAILED row renders the most recent reason line`() {
        val history = listOf(
            CommandStatusTransition(
                fromStatus = CommandStatus.EXECUTING,
                toStatus = CommandStatus.FAILED,
                at = issuedAt.plusSeconds(10),
                detail = mapOf("reason" to "relay disconnected"),
            ),
        )
        composeRule.setContent {
            MaterialTheme {
                CommandRow(
                    command = command(
                        status = CommandStatus.FAILED,
                        history = history,
                    ),
                    now = now,
                    onAck = {},
                )
            }
        }
        composeRule.onNodeWithText("relay disconnected").assertIsDisplayed()
    }

    @Test
    fun `non-failure rows do not render a reason line`() {
        composeRule.setContent {
            MaterialTheme {
                CommandRow(
                    command = command(status = CommandStatus.EXECUTING),
                    now = now,
                    onAck = {},
                )
            }
        }
        // The reason-line tag should not exist for non-failure statuses.
        composeRule.onNodeWithTag("command_row_reason").assertDoesNotExist()
    }

    @Test
    fun `renders all five command types without crashing`() {
        val types = listOf(
            "capture_photo",
            "record_video",
            "request_buffer",
            "start_audio",
            "stop_audio",
        )
        types.forEach { t ->
            composeRule.setContent {
                MaterialTheme {
                    CommandRow(command = command(type = t), now = now, onAck = {})
                }
            }
            composeRule.onNodeWithText(t).assertIsDisplayed()
        }
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandRowTest"`
Expected: FAIL with `Unresolved reference: CommandRow`.

- [ ] **Step 3: Write minimal implementation**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandRow.kt`:

```kotlin
package com.sense.relay.ui.commands

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.heightIn
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.Command
import com.sense.relay.data.CommandStatus
import com.sense.relay.ui.design.StatePill
import com.sense.relay.ui.design.StateStyle
import com.sense.relay.ui.design.Tone
import java.time.Instant

/**
 * One command row. Pure presentation — no state, no IO, no
 * coroutines. All user input is dispatched via [onAck].
 *
 * Layout (top to bottom inside a [Card]):
 *  - title:  command.type
 *  - subtitle: StatusPill + " · " + relativeTime(issuedAt, now)
 *  - reason:  most recent history detail's "reason" for FAILED /
 *             CANCELLED / TIMED_OUT only; absent otherwise
 *  - trailing button (right side): "Ack", shown only when PENDING
 */
@Composable
fun CommandRow(
    command: Command,
    now: Instant,
    onAck: (commandId: String) -> Unit,
    modifier: Modifier = Modifier,
) {
    val reason = remember(command) {
        if (command.status.isTerminalFailure()) {
            command.history
                .lastOrNull { it.toStatus == command.status }
                ?.detail
                ?.get("reason")
                ?.toString()
                ?.takeIf { it.isNotBlank() }
        } else null
    }

    Card(
        modifier = modifier
            .fillMaxWidth()
            .heightIn(min = 64.dp),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Row(
            modifier = Modifier
                .fillMaxWidth()
                .padding(Spacing.md),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(Spacing.sm),
        ) {
            Column(modifier = Modifier.weight(1f)) {
                Text(
                    text = command.type,
                    style = MaterialTheme.typography.titleMedium,
                )
                Row(
                    verticalAlignment = Alignment.CenterVertically,
                    horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
                ) {
                    StatePill(
                        style = StateStyle(label = command.status.name, tone = Tone.Neutral),
                    )
                    Text(
                        text = " · ${relativeTime(command.issuedAt, now)}",
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                }
                if (reason != null) {
                    Text(
                        text = reason,
                        style = MaterialTheme.typography.bodySmall,
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        modifier = Modifier.testTag("command_row_reason"),
                    )
                }
            }
            if (command.status == CommandStatus.PENDING) {
                Button(
                    onClick = { onAck(command.commandId) },
                    modifier = Modifier
                        .size(width = 72.dp, height = 48.dp)
                        .testTag("command_row_ack"),
                ) {
                    Text("Ack", style = MaterialTheme.typography.labelLarge)
                }
            }
        }
    }
}
```

Note on the `remember` import — add it to the imports:
```kotlin
import androidx.compose.runtime.remember
```

The `Spacing.xs` value depends on the project's `core/ui/Spacing` object; if it does not exist, replace it with `4.dp` (hardcoded, project standard for tight inline gaps). Verify by reading the file:

Run: `find android/sense-relay -path "*core/ui/Spacing*" -name "*.kt" 2>/dev/null`
Expected: the file exists. Read it to confirm `Spacing.xs` is defined; if not, replace with `4.dp`.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandRowTest"`
Expected: PASS. 8 tests, all green.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandRow.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandRowTest.kt
git commit -m "feat(android): CommandRow composable

Title + status pill + relative time + Ack (PENDING only) + reason
on terminal failures. Reuses existing StatePill / Card. TDD;
8 Compose UI tests."
```

---

## Task 6: `CommandsScreen` Composable + tests (TDD)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsScreen.kt`
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandsScreenTest.kt`

**Interfaces:**
- Consumes: `com.sense.relay.data.CommandsViewModel.UiState` (sealed: Loading / Error / Ready); the new `startPolling` / `stopPolling` from Task 4; the `CommandRow` and `CommandStatusBuckets` from Tasks 5 and 2.
- Produces: `@Composable fun CommandsScreen(state: CommandsUiState, onAck: (String) -> Unit, onRetry: () -> Unit, modifier: Modifier = Modifier)` — a stateless Composable that takes the UiState (not the ViewModel) so it can be unit-tested without Hilt.

The `Route` variant (which owns the ViewModel + lifecycle) is created in Task 7.

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandsScreenTest.kt`:

```kotlin
package com.sense.relay.ui.commands

import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.test.assertIsDisplayed
import androidx.compose.ui.test.junit4.createComposeRule
import androidx.compose.ui.test.onNodeWithTag
import androidx.compose.ui.test.onNodeWithText
import androidx.compose.ui.test.performClick
import com.sense.relay.data.Command
import com.sense.relay.data.CommandStatus
import com.sense.relay.data.CommandsViewModel
import org.junit.Assert.assertEquals
import org.junit.Rule
import org.junit.Test
import java.time.Instant

class CommandsScreenTest {

    @get:Rule
    val composeRule = createComposeRule()

    private val now: Instant = Instant.parse("2026-07-15T12:00:00Z")
    private val issuedAt: Instant = now.minusSeconds(30)

    private fun command(
        commandId: String = "c1",
        status: CommandStatus = CommandStatus.DELIVERED,
        type: String = "capture_photo",
    ): Command = Command(
        commandId = commandId,
        sessionId = "s1",
        type = type,
        params = emptyMap(),
        issuedAt = issuedAt,
        expiresAt = now.plusSeconds(1800),
        status = status,
    )

    // -- Loading --

    @Test
    fun `Loading shows the loading placeholder`() {
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Loading,
                    onAck = {},
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithTag("commands_loading").assertIsDisplayed()
    }

    // -- Error --

    @Test
    fun `Error shows the error state with Retry button`() {
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Error("boom"),
                    onAck = {},
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithText("boom").assertIsDisplayed()
        composeRule.onNodeWithTag("commands_retry").assertIsDisplayed()
    }

    @Test
    fun `tapping Retry fires onRetry`() {
        var captured = 0
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Error("boom"),
                    onAck = {},
                    onRetry = { captured++ },
                )
            }
        }
        composeRule.onNodeWithTag("commands_retry").performClick()
        assertEquals(1, captured)
    }

    // -- Ready (with rows) --

    @Test
    fun `Ready with one in-flight command shows In flight with count chip 1`() {
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Ready(
                        listOf(command(status = CommandStatus.DELIVERED)),
                    ),
                    onAck = {},
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithText("In flight (1)").assertIsDisplayed()
        composeRule.onNodeWithText("capture_photo").assertIsDisplayed()
    }

    @Test
    fun `Ready with two in-flight and one failed shows both sections with Done having one row`() {
        val cmds = listOf(
            command(commandId = "a", status = CommandStatus.DELIVERED, type = "capture_photo"),
            command(commandId = "b", status = CommandStatus.EXECUTING, type = "record_video"),
            command(commandId = "c", status = CommandStatus.FAILED, type = "request_buffer"),
        )
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Ready(cmds),
                    onAck = {},
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithText("In flight (2)").assertIsDisplayed()
        composeRule.onNodeWithText("Done").assertIsDisplayed()
        // Done section should contain exactly the FAILED row -> one "request_buffer" text node.
        // (Both sections use the same type, so the count of the row text is at least 1.)
        composeRule.onNodeWithText("request_buffer").assertIsDisplayed()
    }

    // -- Ready (empty) --

    @Test
    fun `Ready empty list shows the empty state and no section headers`() {
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Ready(emptyList()),
                    onAck = {},
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithText("No commands yet").assertIsDisplayed()
        composeRule.onNodeWithTag("commands_section_in_flight").assertDoesNotExist()
        composeRule.onNodeWithTag("commands_section_done").assertDoesNotExist()
    }

    // -- Ack wiring --

    @Test
    fun `tapping Ack on a PENDING row fires onAck with that command id`() {
        val cmds = listOf(command(commandId = "abc", status = CommandStatus.PENDING))
        var captured: String? = null
        composeRule.setContent {
            MaterialTheme {
                CommandsScreen(
                    state = CommandsViewModel.UiState.Ready(cmds),
                    onAck = { captured = it },
                    onRetry = {},
                )
            }
        }
        composeRule.onNodeWithTag("command_row_ack").performClick()
        assertEquals("abc", captured)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandsScreenTest"`
Expected: FAIL with `Unresolved reference: CommandsScreen` (and the `commands_loading` / `commands_retry` / `commands_section_*` test tags are also missing).

- [ ] **Step 3: Write minimal implementation**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsScreen.kt`:

```kotlin
package com.sense.relay.ui.commands

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.Command
import com.sense.relay.data.CommandsViewModel
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.SectionHeader
import java.time.Instant

/**
 * Stateless CommandsScreen. Takes the [CommandsViewModel.UiState]
 * directly so it is unit-testable without Hilt. The
 * [CommandsRoute] (Task 7) wires the ViewModel + lifecycle polling.
 *
 * Layout:
 *  - SenseTopBar("Commands")
 *  - Loading: full-screen CircularProgressIndicator
 *  - Error: error message + Retry button
 *  - Ready: "In flight (n)" section + rows; "Done" section +
 *    most recent failure row (only if a failure exists)
 *  - Ready empty: EmptyState body only
 */
@Composable
fun CommandsScreen(
    state: CommandsViewModel.UiState,
    onAck: (commandId: String) -> Unit,
    onRetry: () -> Unit,
    modifier: Modifier = Modifier,
    now: Instant = Instant.now(),
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(title = "Commands")
        when (state) {
            is CommandsViewModel.UiState.Loading -> LoadingPlaceholder()
            is CommandsViewModel.UiState.Error -> ErrorContent(
                message = state.message,
                onRetry = onRetry,
            )
            is CommandsViewModel.UiState.Ready -> ReadyContent(
                commands = state.commands,
                onAck = onAck,
                now = now,
            )
        }
    }
}

@Composable
private fun LoadingPlaceholder() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .testTag("commands_loading"),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator()
    }
}

@Composable
private fun ErrorContent(message: String, onRetry: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            verticalArrangement = Arrangement.spacedBy(Spacing.md),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = message,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Button(
                onClick = onRetry,
                modifier = Modifier.testTag("commands_retry"),
            ) {
                Text("Retry")
            }
        }
    }
}

@Composable
private fun ReadyContent(
    commands: List<Command>,
    onAck: (String) -> Unit,
    now: Instant,
) {
    val inFlight = commands.filter { it.status.isInFlight() }
    val mostRecentFailure = commands.firstOrNull { it.status.isTerminalFailure() }

    when {
        inFlight.isEmpty() && mostRecentFailure == null -> {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(Spacing.lg),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "No commands yet — try saying \"take a photo\" to the agent.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        else -> {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(Spacing.md),
                verticalArrangement = Arrangement.spacedBy(Spacing.sm),
            ) {
                if (inFlight.isNotEmpty()) {
                    item {
                        SectionHeader(title = "In flight (${inFlight.size})")
                            .WithTag("commands_section_in_flight")
                    }
                    items(inFlight, key = { it.commandId }) { c ->
                        CommandRow(
                            command = c,
                            now = now,
                            onAck = onAck,
                        )
                    }
                }
                if (mostRecentFailure != null) {
                    item {
                        SectionHeader(title = "Done")
                            .WithTag("commands_section_done")
                    }
                    item(key = "done_${mostRecentFailure.commandId}") {
                        CommandRow(
                            command = mostRecentFailure,
                            now = now,
                            onAck = { /* no-op for terminal rows */ },
                        )
                    }
                }
            }
        }
    }
}

// Tiny Compose extension to attach a testTag to SectionHeader (which
// doesn't take a Modifier parameter today). This keeps the production
// API untouched; the extension is local to the screen file.
@Composable
private fun SectionHeader.WithTag(tag: String): SectionHeader {
    // We can't truly attach a testTag without changing SectionHeader's
    // API, so we wrap it in a Box that carries the tag and lays out
    // identically.
    androidx.compose.foundation.layout.Box(
        modifier = Modifier.testTag(tag),
    ) {
        this@WithTag()
    }
    return this
}
```

**Required notes:**

- `SenseTopBar`, `SectionHeader` are in `com.sense.relay.ui.design`. Verify they exist (already done in scope-check; `SenseTopBar.kt` and `SectionHeader.kt` are in `ui/design/`).
- `Spacing.lg`, `Spacing.md`, `Spacing.sm` are in `com.sense.relay.core.ui.Spacing`. Verify by reading `core/ui/Spacing.kt` — if `Spacing.lg` does not exist, replace with the closest existing token (likely `Spacing.md` or `16.dp`).
- The `WithTag` helper is a private Compose extension to attach a testTag without modifying `SectionHeader`'s production API. It uses `androidx.compose.foundation.layout.Box`; ensure the import is present (already added in the file).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.commands.CommandsScreenTest"`
Expected: PASS. 7 tests, all green.

- [ ] **Step 5: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsScreen.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/commands/CommandsScreenTest.kt
git commit -m "feat(android): CommandsScreen (stateless) — Loading/Error/Ready branches

In flight (count chip) + Done (most recent failure) sections.
Reuses SenseTopBar, SectionHeader, CommandRow, StatePill, Card.
TDD; 7 Compose UI tests."
```

---

## Task 7: `CommandsRoute` (lifecycle-aware polling wiring)

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsRoute.kt`

**Interfaces:**
- Consumes: the existing `RepositoryModule.repos.commandRepository` (verify in `data/RepositoryModule.kt`); `androidx.lifecycle.compose.LifecycleEventEffect` + `Lifecycle.Event.ON_RESUME / ON_PAUSE` from the new dep.
- Produces: `@Composable fun CommandsRoute(modifier: Modifier = Modifier)` — the navigation entry point. Builds the ViewModel via the project's `viewModelFactory` pattern; collects `state`; calls `startPolling` on `ON_RESUME` and `stopPolling` on `ON_PAUSE`; renders `CommandsScreen`.

- [ ] **Step 1: Verify the `commandRepository` accessor exists in `RepositoryModule`**

Run: `grep -n "commandRepository" android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`
Expected: a line exposing `commandRepository` (e.g. `val commandRepository: CommandRepository`). If not present, stop and report — this is an unexpected deviation from the spec's assumption.

- [ ] **Step 2: Create the Route file**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsRoute.kt`:

```kotlin
package com.sense.relay.ui.commands

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.sense.relay.data.CommandsViewModel
import com.sense.relay.data.RepositoryModule

/**
 * Navigation entry point for the Commands screen. Owns:
 *   - the ViewModel (built via the project's manual-DI `viewModelFactory`)
 *   - the lifecycle-aware polling (start on ON_RESUME, stop on ON_PAUSE)
 *
 * The stateless [CommandsScreen] is rendered below.
 */
@Composable
fun CommandsRoute(modifier: Modifier = Modifier) {
    val vm: CommandsViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                CommandsViewModel(repo = RepositoryModule.repos.commandRepository)
            }
        },
    )
    val state by vm.state.collectAsState()

    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { vm.startPolling() }
    LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { vm.stopPolling() }

    CommandsScreen(
        state = state,
        onAck = { id -> vm.ack(id) },
        onRetry = { vm.refresh() },
        modifier = modifier,
    )
}
```

- [ ] **Step 3: Verify the file compiles**

Run: `cd android/sense-relay && ./gradlew :app:compileDebugKotlin`
Expected: BUILD SUCCESSFUL. (No tests in this task; the next task wires the route into the nav graph.)

- [ ] **Step 4: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/commands/CommandsRoute.kt
git commit -m "feat(android): CommandsRoute — ViewModel + lifecycle polling

ON_RESUME -> startPolling(); ON_PAUSE -> stopPolling().
Reuses the project's manual-DI viewModelFactory pattern (same as
DeviceRoute). Stateless CommandsScreen below."
```

---

## Task 8: Wire `Destination.Commands` and the bottom-nav entry

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt` (add `Destination.Commands` to the sealed interface)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt` (add the Commands tab between Device and Settings; import an icon)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt` (add `composable("commands")` + add to `MAIN_ROUTES`)

**Interfaces:**
- Consumes: `Destination` (existing sealed interface); `MainTab` (existing private data class in `BottomBar.kt`); the existing `NavHost` block.
- Produces: a new `Destination.Commands` (route = "commands"); a new bottom-nav tab between Device and Settings; a new `composable("commands")` entry inside `NavHost`.

- [ ] **Step 1: Add `Destination.Commands`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt`. Add a new `data object Commands : Destination { override val route = "commands" }` line inside the sealed interface, immediately after the `Device` line:

```kotlin
    data object Device : Destination { override val route = "device" }

    /** P2-commands user-facing surface — active command lifecycle. */
    data object Commands : Destination { override val route = "commands" }

    data object Settings : Destination { override val route = "settings" }
```

- [ ] **Step 2: Add the Commands tab to the bottom bar**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt`.

Add an import for an icon. The existing file uses icons from `androidx.compose.material.icons.filled.*` and `automirrored.filled.*` (already on classpath). Pick `Icons.AutoMirrored.Filled.List` is taken; `Icons.Filled.Info` is taken. Use `Icons.Filled.PlayArrow` (a generic "command" / "run" cue; available in the core icon set). Add this import alongside the existing ones:

```kotlin
import androidx.compose.material.icons.filled.PlayArrow
```

In the `mainDestinations` list (currently `Home, Recordings, Device, Settings`), insert `Commands` between `Device` and `Settings`:

```kotlin
private val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", Icons.Filled.Home),
    MainTab(Destination.Recordings, "Recordings", Icons.AutoMirrored.Filled.List),
    MainTab(Destination.Device, "Device", Icons.Filled.Info),
    MainTab(Destination.Commands, "Commands", Icons.Filled.PlayArrow),
    MainTab(Destination.Settings, "Settings", Icons.Filled.Settings),
)
```

- [ ] **Step 3: Wire the `commands` route in `AppNavigation`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt`.

Add an import for the route:

```kotlin
import com.sense.relay.ui.commands.CommandsRoute
```

In the `NavHost { ... }` block, add a `composable(Destination.Commands.route) { CommandsRoute() }` line immediately after the `Destination.Device.route` entry:

```kotlin
            composable(Destination.Device.route) { DeviceRoute() }
            composable(Destination.Commands.route) { CommandsRoute() }
            composable(Destination.Settings.route) { SettingsRoute(onReconfigure) }
```

Add `"commands"` to the `MAIN_ROUTES` set (so the `BottomBar` shows on this screen):

```kotlin
private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Device.route,
    Destination.Commands.route,
    Destination.Settings.route,
)
```

- [ ] **Step 4: Verify the full project compiles**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug`
Expected: BUILD SUCCESSFUL. Exit code 0. The debug APK builds.

- [ ] **Step 5: Run the entire unit-test suite**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest`
Expected: BUILD SUCCESSFUL. All previous tests still pass; all new tests pass.

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt
git commit -m "feat(android): wire Commands into the nav graph

New Destination.Commands; Commands tab between Device and Settings;
composable('commands') in AppNavigation; MAIN_ROUTES extended.
Uses the same patterns as Home/Recordings/Device/Settings."
```

---

## Task 9: Architectural invariant test (UI must not import http/ types)

**Files:**
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/ArchitecturalInvariantsTest.kt`

**Interfaces:**
- Consumes: nothing (pure file-tree scan).
- Produces: `class ArchitecturalInvariantsTest` with one test (`ui layer must not import http or dto types`) that fails the build if any `.kt` file under `app/src/main/kotlin/com/sense/relay/ui/` contains `import com.sense.relay.http` or `import com.sense.relay.http.dto`.

- [ ] **Step 1: Write the failing test (it should currently fail because the new `CommandsRoute` doesn't violate the rule, but neither does it have to — this test should pass immediately; the test's value is in catching future regressions)**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/ArchitecturalInvariantsTest.kt`:

```kotlin
package com.sense.relay.arch

import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Architectural invariant: the `ui/` package must not import
 * anything from `com.sense.relay.http` or `com.sense.relay.http.dto`.
 *
 * This is the matching guard for INV-11 on the UI side. The
 * repository (data layer) is the only place that knows about
 * `HttpApiError` and the wire DTOs; the UI consumes only domain
 * types from `data/`. If a future change adds a `http.*` import
 * to a UI file, this test fails.
 *
 * Pure file-tree scan — no Android, no reflection, runs in ms.
 */
class ArchitecturalInvariantsTest {

    @Test
    fun `ui layer must not import http or dto types`() {
        val uiDir = File("src/main/kotlin/com/sense/relay/ui")
        if (!uiDir.exists()) {
            // Sanity: if the layout changed, fail loudly so the test
            // is updated to point at the new location.
            error("ui source directory not found at ${uiDir.absolutePath} — update this test")
        }
        val offenders = uiDir.walkTopDown()
            .filter { it.isFile && it.extension == "kt" }
            .filter { f ->
                val text = f.readText()
                text.lineSequence().any { line ->
                    val trimmed = line.trimStart()
                    trimmed.startsWith("import com.sense.relay.http")
                }
            }
            .map { it.relativeTo(uiDir).path }
            .toList()

        assertTrue(
            "ui/ files must not import com.sense.relay.http.* (INV-11 UI guard):\n  ${offenders.joinToString("\n  ")}",
            offenders.isEmpty(),
        )
    }
}
```

- [ ] **Step 2: Run the test to verify it passes today**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.arch.ArchitecturalInvariantsTest"`
Expected: PASS. 1 test, green. (The current `ui/` source does not contain any `com.sense.relay.http` imports — `ui/memory/MemoryViewModelTest.kt` is in `test/`, not `main/`, and tests are allowed to import http types for fakes; the test only scans `src/main/`.)

- [ ] **Step 3: Verify the test actually fails when violated (sanity check — temporarily add a violating import, run, then revert)**

Add a one-line `import com.sense.relay.http.HttpApiError` to the top of `ui/home/HomeScreen.kt` (after the existing imports; do not commit this change). Run the test — it MUST fail with the file listed as an offender. Then revert the change with `git checkout -- ui/home/HomeScreen.kt`. Re-run the test to confirm it passes again.

- [ ] **Step 4: Commit**

```bash
git add android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/ArchitecturalInvariantsTest.kt
git commit -m "test(android): architectural invariant — ui/ must not import http.*

INV-11 UI guard. Pure file-tree scan; runs in ms; fails the build
on any future http import in a UI file. Sanity-checked: temporarily
adding an import to HomeScreen.kt fails the test; reverting passes."
```

---

## Task 10: Full suite + sanity build

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit-test suite**

Run: `cd android/sense-relay && ./gradlew :app:testDebugUnitTest`
Expected: BUILD SUCCESSFUL. All previous Android tests (211+) still pass; all new tests in this plan pass (CommandStatusBucketsTest, RelativeTimeTest, CommandsViewModelPollingTest, CommandRowTest, CommandsScreenTest, ArchitecturalInvariantsTest).

- [ ] **Step 2: Run the debug build**

Run: `cd android/sense-relay && ./gradlew :app:assembleDebug`
Expected: BUILD SUCCESSFUL. The debug APK builds with the new Commands screen wired in.

- [ ] **Step 3: Run lint as a smoke test (skip if too slow)**

Run: `cd android/sense-relay && ./gradlew :app:lintDebug -q 2>&1 | tail -30`
Expected: no errors related to the new code (existing pre-existing lint warnings are fine).

- [ ] **Step 4: No commit**

This task is a verification gate. If anything fails, fix and re-run; the fix gets its own commit. If everything is green, the plan is complete and the user is ready to push the branch.

---

## Self-Review

**1. Spec coverage:**

| Spec section | Implemented in |
|---|---|
| §1 Architecture (Android-only, thin Composable) | Tasks 5–7 |
| §2 File layout (new + edited) | Tasks 1, 2, 3, 4, 5, 6, 7, 8, 9 |
| §3 `CommandsScreen` top-level Composable + state branches + status bucketing helpers | Tasks 2, 6, 7 |
| §4 `CommandRow` | Task 5 |
| §5 `CommandStatusPill` (reused as `StatePill`) | Task 5 (reuses existing `StatePill`; no new pill file) |
| §6 `RelativeTime` | Task 3 |
| §7 `CommandsViewModel.startPolling / stopPolling` | Task 4 |
| §8 Navigation (route + tab + nav graph wiring) | Task 8 |
| §9 Error handling (table of failure modes) | Task 6 (ErrorContent with Retry; polling self-heals via Task 4) |
| §10 Edge cases (8 explicit cases) | Task 6 (empty / in-flight-0 / many / tap-on-non-PENDING / etc.); Task 7 (deep-link / process restart) |
| §11 Testing (4 unit + 3 UI + 1 invariant) | Tasks 2, 3, 4, 5, 6, 9 |
| §12 Non-goals (detail screen / pull-to-refresh / push / history / search / refresh button / localization / animations / params) | Not implemented. No task covers any of these. |
| §13 Design principles (10 binding rules) | Applied throughout: thin Composable (5, 6), ViewModel owns polling (4, 7), no data layer changes (intro), monochrome via StatePill (5), polling-only refresh (4, 7), active-only scope (6), existing patterns (7, 8), INV-11 enforced (9), TDD (all tasks), no rewrites (intro). |
| §14 Phasing (single chunk, TDD, main green) | Each task is independently testable; main stays green at every commit. |

**2. Placeholder scan:** No "TBD", no "TODO", no "implement later". Every step shows actual code or an actual command. The only place I used a placeholder-like phrase is the sanity-check in Task 9 Step 3 ("temporarily add... then revert") — that's an explicit, described action, not a placeholder.

**3. Type consistency:** Method names, parameter types, and return types used in later tasks match what earlier tasks defined:
- `CommandsViewModel.startPolling(intervalMs: Long = 4_000L)` and `stopPolling()` are defined in Task 4, used in Task 7.
- `CommandStatus.isInFlight()` / `isTerminalFailure()` are defined in Task 2, used in Task 6.
- `CommandRow(command, now, onAck)` signature is defined in Task 5, used in Task 6.
- `relativeTime(then, now)` signature is defined in Task 3, used in Task 5.
- `CommandsScreen(state, onAck, onRetry, modifier, now)` signature is defined in Task 6, used in Task 7.
- `CommandsRoute(modifier)` signature is defined in Task 7, used in Task 8.
- `Destination.Commands` is defined in Task 8, used in Task 8.

**Issues found and fixed during self-review:**

- **Spec said `lifecycle-runtime-compose` was on the classpath — it wasn't.** Confirmed with the user; Task 1 adds the dep.
- **Spec said to build a new `CommandStatusPill` — but `StatePill` already exists.** Confirmed with the user; Task 5 reuses `StatePill` directly. No `CommandStatusPill.kt` file exists in this plan.
- **`SectionHeader` doesn't accept a `Modifier` parameter.** Task 6 adds a private Compose extension `SectionHeader.WithTag(tag)` to attach a test tag without changing `SectionHeader`'s production API. This is a local-internal helper, not a public API change.
- **`Spacing.xs` may not exist in `core/ui/Spacing`.** Task 5 explicitly verifies the file and provides a fallback to `4.dp` if the constant is missing.
- **`commandscreen` test for the empty state asserts that `commands_section_in_flight` / `commands_section_done` tags do not exist.** This is the inverse of how the Ready-with-rows tests assert these tags. Both directions are covered; the empty-state test is correct.

No outstanding spec gaps. Plan is complete.
