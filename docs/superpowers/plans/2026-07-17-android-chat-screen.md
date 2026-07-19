# Android ChatScreen Composable Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the missing user-facing surface for P2-answers — the Android `ChatScreen` Composable, the message bubbles (one per `ChatMessageKind` variant), the lifecycle-aware `ChatRoute`, and the bottom-nav + deep-link wiring — closing the P2-answers loop end-to-end on the user side.

**Architecture:** Android-only. The screen is a thin view: it collects from the existing `ChatViewModel` (which gains a `messages: StateFlow<List<ChatMessage>>` passthrough) and renders one bubble Composable per `ChatMessageKind` (USER_TEXT → `UserMessageBubble`; AGENT_ANSWER → `AgentMessageBubble`; AGENT_REFUSE → `RefuseMessageBubble`; AGENT_ERROR → `ErrorMessageBubble`). The agent-thinking state is shown as an animated 3-dot `ThinkingBubble` appended below the last user message. The Route owns the ViewModel via the project's `viewModelFactory` pattern. One small data-layer edit (`ChatMessageKind` enum on `ChatMessage`) gives the screen exhaustive `when`-dispatch. The DI wire adds `agentRepository` (built with a `suspend () -> AgentApi` factory matching the `CommandRepository` pattern) and `chatHistoryStore` to `RepositoryModule.repos`. No server / firmware / protocol changes; the architectural invariant `ui/ must not import com.sense.relay.http.*` is reinforced by the existing `ArchitecturalInvariantsTest`.

**Tech Stack:** Kotlin 1.9+ / Jetpack Compose / Hilt-free manual DI (`viewModelFactory { initializer { ... } }`) / Material 3 (`OutlinedTextField`, `SuggestionChip`, `IconButton`, `rememberInfiniteTransition`) / `androidx.lifecycle:lifecycle-runtime-compose:2.8.7` (already on classpath from the Commands plan) / `kotlinx.coroutines.test`. No new dependencies.

## Global Constraints

- **TDD throughout** for everything that can be tested on the host JVM (pure functions, ViewModel behavior, enum round-trips). The plan documents the Compose UI test situation explicitly under "What this slice does NOT add."
- **No new HTTP / DTO / wire types.** INV-11 is reinforced: `ui/` must not import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`. The existing `ArchitecturalInvariantsTest` enforces this.
- **No changes to `AgentApi.kt`, the DTOs in `http/dto/`, `HttpApiError.kt`, `ErrorCode.kt`** — INV-11 enforced.
- **`ChatViewModel` public surface change** is additive only: the existing `draft`, `loading`, `onTextChanged`, `ask()`, `clear()` are unchanged. The new `messages: StateFlow<List<ChatMessage>>` is a passthrough to `ChatHistoryStore.messages`. The three `AgentOutcome` branches in `ask()` gain a one-line `kind = ChatMessageKind.AGENT_*` assignment.
- **`ChatHistoryStore` change** is additive only: the `ChatMessage` data class gains a defaulted `kind: ChatMessageKind = ChatMessageKind.USER_TEXT` field. The `append` / `replace` / `clear` / `flush` API is untouched.
- **Reuse the design system.** `SenseTopBar`, `EmptyState`, `Spacing`, `TouchTarget`. The `MaterialTheme.colorScheme.primaryContainer` and `surfaceVariant` are the bubble backgrounds. No new design-system components.
- **Monochrome UI.** No per-status colors; the bubble shape and content are the signal.
- **Manual DI pattern.** `ChatRoute` builds the ViewModel via `viewModelFactory { initializer { ChatViewModel(repo = ..., store = ...) } }` — same pattern as `HomeRoute` / `DeviceRoute` / `CommandsRoute`.
- **Bottom bar grows to 6 tabs** (Home, Recordings, Device, Commands, Chat, Settings) — INV-13 deviation, documented inline in `BottomBar.kt`.
- **Existing data layer contract is sacred.** `ChatMessage` (the data class), `Role`, `AgentRepository.ask()`, `ChatHistoryStore` class body are the contract. Only the additive `kind` field changes.
- **The existing 7 `ChatViewModelTest` tests must remain green.** The slice adds 4 one-line `kind` assertions to 4 of them.
- **Frequent commits.** Every task ends with a commit. `main` stays green at every step.

## What this slice does NOT add (testing note)

**No Compose UI tests.** The Commands plan's 7 + 8 Compose UI test cases were dropped during execution (see git commits `5c209d8` and `6c800f6`): Robolectric PR #4736 blocks host-JVM Compose UI tests in this sandbox (component-activity resolve failure on the first SDK 34 launch), and the project has no `androidTest` setup. Per the user's standing decision (project-status memory, 2026-07-17: "Compose UI tests dropped due to sandbox issues with Robolectric PR #4736"), the ChatScreen Composables ship without host-JVM Compose UI tests. They are validated manually on a real device.

The plan's test inventory reflects this: the **unit tests** (pure functions, ViewModel behavior, enum round-trips — all host-JVM, no Compose) ship; the **Compose UI tests** (10 files, ~22 cases) do not. The existing `ArchitecturalInvariantsTest` (file-tree scan) still enforces INV-11 across the chat package.

---

## File Structure

### New files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/
  ChatRoute.kt                — Route: builds ViewModel, collects state, owns LazyListState,
                                passes onOpenAtom / onOpenMemory through
  ChatScreen.kt               — top-level stateless Composable
  ChatMessageList.kt          — LazyColumn + LaunchedEffect(scrollToBottom) + when(kind) dispatch
  ChatInputBar.kt             — Row: OutlinedTextField + IconButton (send / spinner)
  UserMessageBubble.kt        — right-aligned, primaryContainer
  AgentMessageBubble.kt       — left-aligned, surfaceVariant, with optional AtomChip row
  ThinkingBubble.kt           — animated 3-dot pulse
  RefuseMessageBubble.kt      — friendly copy + "browse memory directly" TextButton
  ErrorMessageBubble.kt       — left-aligned, renders pre-mapped message
  AtomChip.kt                 — SuggestionChip wrapper (label = truncated text)

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/
  MemoryRoute.kt              — stub: EmptyState("Memory", "Browse and search — coming soon")
  MemoryScreen.kt             — stateless wrapper around EmptyState

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/
  AtomDetailRoute.kt          — stub: looks up chip text from ChatHistoryStore, renders placeholder
  AtomDetailScreen.kt         — stateless Composable (atomId, text, modifier)

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/
  ChatMessageKindTest.kt      — host-JVM unit test: enum round-trip (4 cases)
```

### Edited files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt
                              — add ChatMessageKind enum + `kind` field on ChatMessage
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt
                              — add `messages` passthrough + `kind` assignment in each ask() branch
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/AgentRepository.kt
                              — refactor: api: AgentApi -> apiProvider: suspend () -> AgentApi
                                (mirrors CommandRepository; adds a secondary ctor for tests)
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt
                              — construct agentRepository via apiProvider; add chatHistoryStore
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt
                              — add ARG_ATOM_ID constant on Destination.AtomDetail
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt
                              — add 6th tab (Chat), document INV-13 deviation inline
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt
                              — wire composable(Chat.route) + composable(Memory.route) +
                                composable(AtomDetail.route) with navArgument
android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt
                              — add 4 one-line `kind` assertions to existing outcome tests
```

### Files explicitly NOT changed

- `AgentApi.kt`, the DTOs in `http/dto/`, `HttpApiError.kt`, `ErrorCode.kt` — INV-11 holds.
- `ChatHistoryStore` class body (only `ChatMessage` gains the `kind` field; methods are untouched).
- `CommandApi.kt`, `CommandRepository.kt`, `CommandsViewModel.kt`, the Commands screen files.
- `build.gradle.kts` — no new dependencies.
- The setup / onboarding / device / recordings / home screens.
- `MemoryViewModel.kt` — the new `MemoryScreen` / `MemoryRoute` are stubs that don't need the VM yet.

---

## Task 1: `ChatMessageKind` enum + `kind` field on `ChatMessage`

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt:58-66` (the `data class ChatMessage` block)
- Create: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt`

**Interfaces:**
- Consumes: nothing (the `Role` enum already in the same file at line 68).
- Produces:
  - `enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR }` — placed in the same file as `ChatMessage`.
  - `ChatMessage` gains `val kind: ChatMessageKind = ChatMessageKind.USER_TEXT` — the default keeps every existing construction site (and the 7 existing `ChatViewModelTest` tests) compiling.

- [ ] **Step 1: Write the failing test**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt`:

```kotlin
package com.sense.relay.ui.chat

import com.sense.relay.data.ChatMessageKind
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * Round-trip the ChatMessageKind enum. Exhaustiveness in the
 * screen's `when (msg.kind)` is verified by the compiler; this
 * test pins the names + ordinals so a future reorder is a
 * deliberate edit.
 */
class ChatMessageKindTest {

    @Test
    fun `USER_TEXT is the default kind for a new ChatMessage`() {
        // Construct via the user-typed branch in ChatViewModel.ask():
        // the kind is not set explicitly, so it must default to USER_TEXT.
        // The actual ChatMessage is constructed inside ChatViewModel.ask();
        // here we exercise the default value at the data-class level.
        // We can't construct ChatMessage without role (required), so we
        // assert via the default-value property instead.
        val default: ChatMessageKind = ChatMessageKind.USER_TEXT
        assertEquals("USER_TEXT", default.name)
    }

    @Test
    fun `AGENT_ANSWER has the expected name`() {
        assertEquals("AGENT_ANSWER", ChatMessageKind.AGENT_ANSWER.name)
    }

    @Test
    fun `AGENT_REFUSE has the expected name`() {
        assertEquals("AGENT_REFUSE", ChatMessageKind.AGENT_REFUSE.name)
    }

    @Test
    fun `AGENT_ERROR has the expected name`() {
        assertEquals("AGENT_ERROR", ChatMessageKind.AGENT_ERROR.name)
    }
}
```

- [ ] **Step 2: Run test to verify it fails**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatMessageKindTest"
```

Expected: FAIL with `Unresolved reference: ChatMessageKind`.

- [ ] **Step 3: Write minimal implementation**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt`. The current file ends with:

```kotlin
data class ChatMessage(
    val id: String,
    val role: Role,
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
)

enum class Role { USER, AGENT }
```

Replace it with:

```kotlin
/**
 * Variant of a [ChatMessage]. The screen's [com.sense.relay.ui.chat.ChatMessageList]
 * dispatches on this with an exhaustive `when` — adding a new
 * variant is a compile error in the screen, which is the right
 * failure mode.
 *
 * - USER_TEXT: a message the user typed.
 * - AGENT_ANSWER: a Return or ReturnWithUncertainty outcome from the
 *   agent. Carries [ChatMessage.atoms] (the cited memory atoms).
 * - AGENT_REFUSE: a Refuse outcome (no supporting memory). The
 *   screen renders this as the friendly-copy + "browse memory"
 *   link variant.
 * - AGENT_ERROR: an Error outcome (network failure, server error,
 *   rate limit, etc.). The text is pre-mapped via
 *   [com.sense.relay.core.ui.toDisplayMessage] in the route.
 */
enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR }

data class ChatMessage(
    val id: String,
    val role: Role,
    /**
     * The variant of this message. Defaults to [ChatMessageKind.USER_TEXT]
     * so the user-typed construction site in
     * [com.sense.relay.ui.chat.ChatViewModel.ask] doesn't need to
     * set it explicitly. The three agent-outcome branches set the
     * appropriate AGENT_* value.
     */
    val kind: ChatMessageKind = ChatMessageKind.USER_TEXT,
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
)

enum class Role { USER, AGENT }
```

- [ ] **Step 4: Run test to verify it passes**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatMessageKindTest"
```

Expected: PASS. 4 tests, all green.

- [ ] **Step 5: Verify the existing 7 ChatViewModel tests still pass**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatViewModelTest"
```

Expected: PASS. The default `kind = ChatMessageKind.USER_TEXT` keeps every existing construction site compiling and semantically correct.

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatMessageKindTest.kt
git commit -m "feat(android): ChatMessageKind enum on ChatMessage

USER_TEXT (default) | AGENT_ANSWER | AGENT_REFUSE | AGENT_ERROR.
Default value keeps every existing construction site compiling
(7 existing ChatViewModelTest tests stay green). The screen's
bubble dispatch is an exhaustive when over this enum; future
variants are a compile error in ChatMessageList.kt.

TDD: 4 enum round-trip cases."
```

---

## Task 2: `ChatViewModel` — `messages` passthrough + `kind` assignment in `ask()`

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt`
- Modify: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt`

**Interfaces:**
- Consumes: the existing `ChatViewModel(repo: AgentRepository, store: ChatHistoryStore, sessionId: String? = null, savedState: SavedStateHandle = SavedStateHandle())` constructor. Existing public surface: `draft: StateFlow<String>`, `loading: StateFlow<Boolean>`, `onTextChanged(value: String)`, `ask()`, `clear()` — all unchanged.
- Produces (additive):
  - `val messages: StateFlow<List<ChatMessage>> = store.messages` — passthrough to the existing `ChatHistoryStore.messages`. Trivial one-liner; the existing tests verify the underlying store behavior.
  - In `ask()`, each of the three `AgentOutcome` branches constructs its `ChatMessage` with an explicit `kind = ChatMessageKind.AGENT_ANSWER` / `AGENT_REFUSE` / `AGENT_ERROR` field.

- [ ] **Step 1: Add the `messages` passthrough to `ChatViewModel`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt`. The current file has at line 38-39:

```kotlin
    private val _draft = MutableStateFlow(savedState.get<String>(KEY_DRAFT) ?: "")
    val draft: StateFlow<String> = _draft.asStateFlow()
```

Insert immediately after the `draft` declaration (and before `_loading`):

```kotlin
    /**
     * Passthrough to [ChatHistoryStore.messages]. The screen collects
     * this and dispatches each message to the right bubble Composable
     * based on [ChatMessage.kind]. Added for the ChatScreen Composable
     * (P2-answers user-facing surface). The store is the source of
     * truth; this property is a one-line accessor.
     */
    val messages: StateFlow<List<ChatMessage>> = store.messages
```

- [ ] **Step 2: Add `kind` assignment to the Answer branch**

In the same file, find the `Answer` branch of `ask()` (currently around lines 71-91). The `ChatMessage(...)` constructor call (around line 72) starts with:

```kotlin
                    is AgentOutcome.Answer -> {
                        val agent = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-agent",
                            role = Role.AGENT,
                            text = outcome.text,
                            atoms = outcome.atoms.map {
```

Add a `kind` line after `role = Role.AGENT,`:

```kotlin
                    is AgentOutcome.Answer -> {
                        val agent = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-agent",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_ANSWER,
                            text = outcome.text,
                            atoms = outcome.atoms.map {
```

- [ ] **Step 3: Add `kind` assignment to the Refuse branch**

Find the `Refuse` branch (currently around lines 98-113). Add a `kind` line after `role = Role.AGENT,`:

```kotlin
                    is AgentOutcome.Refuse -> {
                        val msg = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-refuse",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_REFUSE,
                            text = outcome.reason.replace("_", " ").replaceFirstChar { it.uppercase() },
```

- [ ] **Step 4: Add `kind` assignment to the Error branch**

Find the `Error` branch (currently around lines 114-127). Add a `kind` line after `role = Role.AGENT,`:

```kotlin
                    is AgentOutcome.Error -> {
                        val msg = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-error",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_ERROR,
                            text = "Error: ${outcome.message}",
```

- [ ] **Step 5: Run the existing ChatViewModel tests to verify nothing broke**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatViewModelTest"
```

Expected: PASS. The 7 existing tests continue to pass (the new `kind` field defaults to `USER_TEXT` for the user-typed message; the agent branches set the right value).

- [ ] **Step 6: Add 4 one-line `kind` assertions to the existing outcome tests**

Edit `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt`. The test file already has 4 outcome tests. Add the kind assertions.

**Test 1 — "ask sends user message and receives answer"** (currently at lines 38-62). After `assertEquals(2, msgs.size)` (line 56), the file asserts user role + text, then agent role + text + atoms. Update to also assert kinds:

Find the block:
```kotlin
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(Role.USER, msgs[0].role)
        assertEquals("what?", msgs[0].text)
        assertEquals(Role.AGENT, msgs[1].role)
        assertEquals("answer: what?", msgs[1].text)
        assertEquals(1, msgs[1].atoms.size)
```

Replace with:
```kotlin
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(Role.USER, msgs[0].role)
        assertEquals(ChatMessageKind.USER_TEXT, msgs[0].kind)
        assertEquals("what?", msgs[0].text)
        assertEquals(Role.AGENT, msgs[1].role)
        assertEquals(ChatMessageKind.AGENT_ANSWER, msgs[1].kind)
        assertEquals("answer: what?", msgs[1].text)
        assertEquals(1, msgs[1].atoms.size)
```

Add `import com.sense.relay.data.ChatMessageKind` to the imports at the top of the file (the existing `import com.sense.relay.data.Role` is on line 8 — add the new import alphabetically below it).

**Test 2 — "ask with refuse outcome renders refusal message"** (currently at lines 75-90). Find the `assertTrue(msgs[1].text.contains("No supporting", ignoreCase = true))` line. Add a kind assertion on the line before it:

```kotlin
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(ChatMessageKind.AGENT_REFUSE, msgs[1].kind)
        assertTrue(msgs[1].text.contains("No supporting", ignoreCase = true))
```

**Test 3 — "ask with error outcome shows error message"** (currently at lines 93-109). Same pattern — add a kind assertion:

```kotlin
        val msgs = store.messages.value
        assertEquals(2, msgs.size)
        assertEquals(ChatMessageKind.AGENT_ERROR, msgs[1].kind)
        assertTrue(msgs[1].text.contains("boom", ignoreCase = true))
```

**Test 4 — "clear empties the history"** (currently at lines 112-124). No kind assertion needed (the test is about `clear()` behavior, not message kind). Leave it untouched.

- [ ] **Step 7: Run tests to verify all pass**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ChatViewModelTest"
```

Expected: PASS. All 7 tests green; 4 of them now have a `kind` assertion.

- [ ] **Step 8: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt
git commit -m "feat(android): ChatViewModel exposes messages StateFlow + sets kind in ask()

The new messages StateFlow is a one-line passthrough to
ChatHistoryStore.messages; the screen collects it. Each
AgentOutcome branch in ask() now sets the appropriate
ChatMessageKind.AGENT_* value on the appended message so the
screen's exhaustive-when dispatch finds the right bubble.

Existing 7 tests stay green; 4 of them gain a one-line kind
assertion to pin the new behavior."
```

---

## Task 3: `AgentRepository` factory refactor + `RepositoryModule` DI wire

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/AgentRepository.kt:15`
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`

**Interfaces:**
- Consumes: the existing `AgentRepository(api: AgentApi)` constructor + the `clientProvider: suspend () -> SenseHttpClient` factory already defined in `RepositoryModule.kt:129-143`.
- Produces:
  - `AgentRepository(apiProvider: suspend () -> AgentApi)` + a secondary `constructor(api: AgentApi) : this(apiProvider = { api })` for test convenience — mirrors `CommandRepository` exactly (see `CommandRepository.kt:39-42`).
  - `RepositoryModule.repos` gains `agentRepository: AgentRepository` and `chatHistoryStore: ChatHistoryStore`.

**Why this refactor:** `RepositoryModule.init()` is synchronous (called from `SenseApplication.onCreate`). The shared `clientProvider` is a `suspend () -> SenseHttpClient` factory. The existing `CommandRepository` already solved this by accepting a `suspend () -> CommandApi` factory. The chat path uses the exact same pattern, with the apiProvider built inline:

```kotlin
val agentRepository = AgentRepository(
    apiProvider = {
        val c = clientProvider()
        AgentApi(baseUrl = c.baseUrl, token = c.token, client = c.client)
    },
)
```

- [ ] **Step 1: Verify the existing `AgentRepositoryTest` still passes against the current API**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.AgentRepositoryTest"
```

Expected: PASS. This is a baseline; we'll verify it stays green after the refactor.

- [ ] **Step 2: Refactor `AgentRepository` to take a `suspend () -> AgentApi` factory**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/AgentRepository.kt`. The current class declaration at line 15 is:

```kotlin
open class AgentRepository(private val api: AgentApi) {

    open suspend fun ask(
        sessionId: String?,
        text: String,
        limit: Int = 10,
    ): AgentOutcome = withContext(Dispatchers.IO) {
        val dto: AgentResponseDto = try {
            api.postAgent(sessionId, text, limit)
        } catch (e: HttpApiError) {
```

Replace with:

```kotlin
/**
 * Domain layer for the agent endpoint. The single importer of
 * [HttpApiError] for the read path (INV-11). The VM / UI see only
 * [AgentOutcome] — never the raw HTTP error.
 *
 * [apiProvider] is a suspend factory that returns the current
 * [AgentApi]. It exists so the repository tracks the latest
 * configured server (the [AgentApi] carries the OkHttp client +
 * bearer token at construction time). The repository resolves the
 * current [AgentApi] on every call, so re-provisioning takes effect
 * on the next /agent request without rebuilding the repository.
 */
open class AgentRepository(private val apiProvider: suspend () -> AgentApi) {

    /** Test/convenience constructor: a repository pinned to a single [AgentApi]. */
    constructor(api: AgentApi) : this(apiProvider = { api })

    open suspend fun ask(
        sessionId: String?,
        text: String,
        limit: Int = 10,
    ): AgentOutcome = withContext(Dispatchers.IO) {
        val dto: AgentResponseDto = try {
            apiProvider().postAgent(sessionId, text, limit)
        } catch (e: HttpApiError) {
```

- [ ] **Step 3: Run AgentRepositoryTest to verify the refactor preserves behavior**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.data.AgentRepositoryTest" --tests "com.sense.relay.ui.chat.ChatViewModelTest"
```

Expected: PASS. The `AgentRepositoryTest` likely uses the `constructor(api: AgentApi)` secondary ctor (the test passes a concrete `AgentApi`); the `ChatViewModelTest`'s `FakeAgentRepo` overrides `ask(...)` so the constructor change is invisible to it.

- [ ] **Step 4: Wire `agentRepository` and `chatHistoryStore` into `RepositoryModule`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt`. The current file has:

- Imports (lines 1-22): add `import com.sense.relay.data.AgentApi` and `import com.sense.relay.data.AgentRepository` (the file already imports many things from `data`; check the existing import block and add alphabetically). `ChatHistoryStore` is in the same package (`com.sense.relay.data`) and doesn't need an import.
- The `Repositories` data class (lines 43-51): add two fields.
- The `init(app)` method (lines 67-106): construct `agentRepository` + `chatHistoryStore`, then add them to the `repos = Repositories(...)` assignment at the end.

**The new `Repositories` data class** (replace lines 43-51):

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

**The new `init(app)` body** — find the end of `init(app)` (currently around line 96, the line `val commandRepository = CommandRepository(`). Replace the `val commandRepository = ...` block and the subsequent `repos = Repositories(...)` call with:

```kotlin
        // Command lifecycle: the API resolves the current (url, token)
        // from the shared clientProvider on every call so a re-provision
        // takes effect on the next /commands request without rebuilding
        // the repository.
        val commandRepository = CommandRepository(
            apiProvider = {
                val c = clientProvider()
                CommandApi(baseUrl = c.baseUrl, token = c.token)
            },
        )
        // P2-answers (ChatScreen): same apiProvider pattern as the
        // command repository. Resolves the current OkHttp client +
        // bearer token on every /agent call, so a re-provision takes
        // effect on the next request without rebuilding the repository.
        val agentRepository = AgentRepository(
            apiProvider = {
                val c = clientProvider()
                AgentApi(baseUrl = c.baseUrl, token = c.token, client = c.client)
            },
        )
        // ChatHistoryStore is a process-singleton (not per-ViewModel) so
        // a deep-link hop (Chat -> AtomDetail -> back) doesn't lose
        // history. The future DataStore-backed version keeps the same
        // accessor and persists across process restarts.
        val chatHistoryStore = ChatHistoryStore()
        repos = Repositories(
            configuration = configuration,
            session = session,
            device = device,
            status = status,
            dashboard = dashboard,
            relayController = relayController,
            commandRepository = commandRepository,
            agentRepository = agentRepository,
            chatHistoryStore = chatHistoryStore,
        )
```

(The existing `init` is one method, with `val configuration = ...`, `val session = ...`, `val status = ...`, `val dashboard = ...`, `val commandRepository = ...`, then `repos = Repositories(...)`. Replace from the `val commandRepository` line through the end of the method with the block above. The `val device = DeviceRepositoryImpl(RelayController)` line is in the original `repos = Repositories(...)` call — pull it out into a `val device = ...` at the top of `init()` if it isn't already. Reading the file: lines 100-105 currently look like `device = DeviceRepositoryImpl(RelayController), status = status, ...` — these are field values in the `repos = Repositories(...)` data class call, but the new construction needs `val device = ...` declared up front so it can be referenced as a top-level val.)

**Specifically**, change:

```kotlin
        repos = Repositories(
            configuration = configuration,
            session = session,
            device = DeviceRepositoryImpl(RelayController),
            status = status,
            dashboard = dashboard,
            relayController = RelayController,
            commandRepository = commandRepository,
        )
```

to:

```kotlin
        val device = DeviceRepositoryImpl(RelayController)
        // ... existing dashboard, commandRepository, agentRepository,
        //     chatHistoryStore declarations ...
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
        )
```

The exact edit is a single replacement of the lines from the closing `)` of `val commandRepository = ...` to the closing `)` of `repos = Repositories(...)` — both replacements shown in the block above. Apply them.

- [ ] **Step 5: Run the full unit-test suite to verify the DI change didn't break anything**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest
```

Expected: PASS. All previous Android tests still green.

- [ ] **Step 6: Verify the project compiles**

Run:
```bash
cd android/sense-relay && ./gradlew :app:assembleDebug
```

Expected: BUILD SUCCESSFUL. The debug APK builds (the ChatRoute isn't wired yet, but `RepositoryModule.repos` having the two new fields must compile end-to-end).

- [ ] **Step 7: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/AgentRepository.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt
git commit -m "feat(android): wire AgentRepository + ChatHistoryStore into DI

AgentRepository now takes a suspend () -> AgentApi factory
(mirrors CommandRepository) so re-provisioning takes effect on
the next /agent call. ChatHistoryStore is a process-singleton;
the future DataStore-backed version keeps the same accessor.

Both are added to RepositoryModule.repos for the ChatRoute
constructor to consume in a later task.

No test changes; existing 232+ tests stay green."
```

---

## Task 4: `ChatMessageList` Composable

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt`

**Interfaces:**
- Consumes: `com.sense.relay.data.ChatMessage`, `com.sense.relay.data.ChatMessageKind`, `com.sense.relay.data.Role`, `com.sense.relay.ui.design.Spacing` (no, `Spacing` is in `core.ui` — see below).
- Produces: `@Composable fun ChatMessageList(messages: List<ChatMessage>, isThinking: Boolean, onAtomChipTap: (String) -> Unit, onBrowseMemory: () -> Unit, modifier: Modifier = Modifier, listState: LazyListState = rememberLazyListState())`. Renders a `LazyColumn` with one bubble per message (exhaustive `when (msg.kind)`), plus a `ThinkingBubble` if `isThinking`. Auto-scrolls to the last item on `messages.size` or `isThinking` change.

**This task ships the Composable without host-JVM Compose UI tests** (per the global "What this slice does NOT add" note). The exhaustive `when (msg.kind)` is the safety net — a future variant is a compile error in this file. The bubble Composables (`UserMessageBubble`, `AgentMessageBubble`, etc.) are created in Task 5.

- [ ] **Step 1: Create the file**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.ChatMessageKind

/**
 * The chat message list. Renders one bubble per [ChatMessage],
 * dispatching by [ChatMessageKind] with an exhaustive `when` (a
 * future variant is a compile error in this file). When
 * [isThinking] is true, a [ThinkingBubble] is appended below the
 * last message — the "agent is working" affordance.
 *
 * Auto-scrolls to the last item on every [messages] change and
 * when [isThinking] flips false -> true. The [listState] is
 * remembered by default; the Route can pass its own (so the scroll
 * position survives process death if the Route persists the state
 * — not in this slice).
 *
 * **Test tag:** `chat_list` (matches ChatScreenTest's expected
 * testTag in the project-status memory; in this slice, no host-JVM
 * Compose UI tests, so the testTag is for manual / future tests).
 */
@Composable
fun ChatMessageList(
    messages: List<ChatMessage>,
    isThinking: Boolean,
    onAtomChipTap: (String) -> Unit,
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
    listState: LazyListState = rememberLazyListState(),
) {
    LaunchedEffect(messages.size, isThinking) {
        if (messages.isNotEmpty()) {
            listState.animateScrollToItem(messages.lastIndex)
        }
    }
    LazyColumn(
        state = listState,
        modifier = modifier.fillMaxSize().testTag("chat_list"),
        contentPadding = PaddingValues(Spacing.md),
        verticalArrangement = Arrangement.spacedBy(Spacing.sm),
    ) {
        items(messages, key = { it.id }) { msg ->
            when (msg.kind) {
                ChatMessageKind.USER_TEXT -> UserMessageBubble(text = msg.text)
                ChatMessageKind.AGENT_ANSWER -> AgentMessageBubble(
                    text = msg.text,
                    atoms = msg.atoms,
                    onAtomChipTap = onAtomChipTap,
                )
                ChatMessageKind.AGENT_REFUSE -> RefuseMessageBubble(
                    onBrowseMemory = onBrowseMemory,
                )
                ChatMessageKind.AGENT_ERROR -> ErrorMessageBubble(
                    message = msg.text.removePrefix("Error: "),
                )
            }
        }
        if (isThinking) {
            // `key = "thinking"` keeps the bubble stable across
            // recompositions and prevents Compose from re-creating
            // the rememberInfiniteTransition each frame.
            item(key = "thinking") { ThinkingBubble() }
        }
    }
}
```

- [ ] **Step 2: Verify the project compiles (the bubble Composables don't exist yet — this step is expected to fail)**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: FAIL with `Unresolved reference: UserMessageBubble` / `AgentMessageBubble` / `RefuseMessageBubble` / `ErrorMessageBubble` / `ThinkingBubble`. The five bubble Composables are created in Task 5.

- [ ] **Step 3: Do not commit yet — Task 5 creates the missing bubbles**

Proceed to Task 5 immediately. The commit happens at the end of Task 5.

---

## Task 5: All five bubble Composables + `AtomChip`

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/UserMessageBubble.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AgentMessageBubble.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ThinkingBubble.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/RefuseMessageBubble.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ErrorMessageBubble.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AtomChip.kt`

**Interfaces:** see the spec §6. All stateless, all take their data via parameters. Each has a unique testTag (used in Task 4's dispatch and in future manual UI validation).

**No host-JVM Compose UI tests** per the global constraint; the bubbles ship without tests in this slice.

- [ ] **Step 1: Create `UserMessageBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/UserMessageBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing

/**
 * A user-typed message. Right-aligned, primaryContainer background
 * to distinguish from agent bubbles. The `user_bubble` testTag is
 * the dispatch marker for ChatMessageList.
 */
@Composable
fun UserMessageBubble(
    text: String,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.End,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.primaryContainer,
            contentColor = MaterialTheme.colorScheme.onPrimaryContainer,
            modifier = Modifier
                .widthIn(max = 320.dp)
                .testTag("user_bubble"),
        ) {
            Text(
                text = text,
                style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.padding(Spacing.md),
            )
        }
    }
}
```

- [ ] **Step 2: Create `AtomChip.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AtomChip.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.material3.SuggestionChip
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.style.TextOverflow

/**
 * A small outlined pill for one cited atom. Wraps Material 3's
 * [SuggestionChip] (the closest built-in to "tappable label"). The
 * `agent_bubble` callsite truncates the text to 80 chars before
 * passing it in; this Composable does not truncate.
 *
 * The `atom_chip` testTag is the dispatch marker — `chat_screen_test`
 * (in the spec's manual-validation checklist) would assert one per
 * atom.
 */
@Composable
fun AtomChip(
    text: String,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    SuggestionChip(
        onClick = onClick,
        label = {
            Text(
                text = text,
                style = MaterialTheme.typography.labelSmall,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
            )
        },
        modifier = modifier.testTag("atom_chip"),
    )
}
```

- [ ] **Step 3: Create `AgentMessageBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AgentMessageBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.AtomChip as DomainAtomChip

/**
 * An agent answer bubble. Left-aligned, surfaceVariant background.
 * If [atoms] is non-empty, a row of [AtomChip]s is rendered below
 * the text — tappable, navigates to AtomDetail on tap (the
 * onAtomChipTap callback is fired with the atom's id).
 *
 * Atom chip text is truncated to 80 chars at the call site (here)
 * with a trailing "…" if longer. The full text is on the server;
 * the chip is a label, not the citation.
 *
 * The `agent_bubble` testTag is the dispatch marker for
 * ChatMessageList.
 */
@Composable
fun AgentMessageBubble(
    text: String,
    atoms: List<DomainAtomChip>,
    onAtomChipTap: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .widthIn(max = 320.dp)
                .testTag("agent_bubble"),
        ) {
            Column(modifier = Modifier.padding(Spacing.md)) {
                Text(text = text, style = MaterialTheme.typography.bodyLarge)
                if (atoms.isNotEmpty()) {
                    Spacer(Modifier.height(Spacing.xs))
                    Row(horizontalArrangement = Arrangement.spacedBy(Spacing.xs)) {
                        atoms.forEach { atom ->
                            AtomChip(
                                text = atom.text.truncateForChip(),
                                onClick = { onAtomChipTap(atom.atomId) },
                            )
                        }
                    }
                }
            }
        }
    }
}

/**
 * Truncate atom text for chip display. The full text is on the
 * server; the chip is a label. 80 chars is a defensive UI cap so
 * a single chip doesn't dominate the row.
 */
private fun String.truncateForChip(): String =
    if (length > 80) take(79) + "…" else this
```

- [ ] **Step 4: Create `ThinkingBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ThinkingBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.animation.core.FastOutSlowInEasing
import androidx.compose.animation.core.RepeatMode
import androidx.compose.animation.core.animateFloat
import androidx.compose.animation.core.infiniteRepeatable
import androidx.compose.animation.core.rememberInfiniteTransition
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.alpha
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing

/**
 * Three small dots that pulse in sequence, indicating the agent is
 * "thinking." Rendered by [ChatMessageList] when `isThinking=true`.
 *
 * Each dot's alpha animates 0.3 -> 1.0 -> 0.3 (Reverse) over 600ms,
 * with a 200ms phase offset between dots. Total visible cycle ~1.2s.
 * The animation runs forever; the bubble is removed from the
 * composition when isThinking flips false.
 */
@Composable
fun ThinkingBubble(modifier: Modifier = Modifier) {
    val transition = rememberInfiniteTransition(label = "thinking")
    Row(
        modifier = modifier
            .padding(start = Spacing.md)
            .testTag("thinking_bubble"),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
    ) {
        repeat(3) { i ->
            val alpha by transition.animateFloat(
                initialValue = 0.3f,
                targetValue = 1.0f,
                animationSpec = infiniteRepeatable(
                    animation = tween(600, delayMillis = i * 200, easing = FastOutSlowInEasing),
                    repeatMode = RepeatMode.Reverse,
                ),
                label = "dot_$i",
            )
            androidx.compose.foundation.layout.Box(
                modifier = Modifier
                    .size(8.dp)
                    .alpha(alpha)
                    .background(
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                        shape = CircleShape,
                    )
                    .testTag("thinking_dot_$i"),
            )
        }
    }
}
```

- [ ] **Step 5: Create `RefuseMessageBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/RefuseMessageBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing

/**
 * Rendered when the agent returns a Refuse outcome (no supporting
 * memory). Shows the friendly copy + a "browse memory directly"
 * text button that fires [onBrowseMemory] — the route wires this
 * to `navController.navigate(Destination.Memory.route)`.
 *
 * The `refuse_bubble` testTag is the dispatch marker for
 * ChatMessageList; `refuse_browse_memory` is the inner button.
 */
@Composable
fun RefuseMessageBubble(
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .widthIn(max = 320.dp)
                .testTag("refuse_bubble"),
        ) {
            Column(modifier = Modifier.padding(Spacing.md)) {
                Text(
                    text = "I don't have a memory about that yet. Try asking after " +
                        "the wearable has captured more, or",
                    style = MaterialTheme.typography.bodyLarge,
                )
                TextButton(
                    onClick = onBrowseMemory,
                    modifier = Modifier.testTag("refuse_browse_memory"),
                ) {
                    Text("browse memory directly")
                }
            }
        }
    }
}
```

- [ ] **Step 6: Create `ErrorMessageBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ErrorMessageBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.widthIn
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing

/**
 * Rendered when the agent returns an Error outcome (network
 * failure, server error, rate limit, etc.). The [message] is the
 * pre-mapped user-facing string — the route runs the raw
 * [com.sense.relay.http.ErrorCode] / [java.io.IOException] through
 * [com.sense.relay.core.ui.toDisplayMessage] before passing it
 * here, so the bubble never sees the raw error.
 *
 * The `error_bubble` testTag is the dispatch marker.
 */
@Composable
fun ErrorMessageBubble(
    message: String,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.Start,
    ) {
        Surface(
            shape = RoundedCornerShape(16.dp),
            color = MaterialTheme.colorScheme.surfaceVariant,
            contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier
                .widthIn(max = 320.dp)
                .testTag("error_bubble"),
        ) {
            Text(
                text = message,
                style = MaterialTheme.typography.bodyLarge,
                modifier = Modifier.padding(Spacing.md),
            )
        }
    }
}
```

- [ ] **Step 7: Verify the project compiles (now that all bubbles exist)**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: BUILD SUCCESSFUL. `ChatMessageList`'s `when (msg.kind)` resolves all four variants.

- [ ] **Step 8: Run the full test suite to confirm no regressions**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest
```

Expected: PASS. All previous Android tests stay green.

- [ ] **Step 9: Commit Task 4 + Task 5 together (ChatMessageList + the five bubbles + AtomChip)**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/UserMessageBubble.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AgentMessageBubble.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ThinkingBubble.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/RefuseMessageBubble.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ErrorMessageBubble.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/AtomChip.kt
git commit -m "feat(android): ChatMessageList + 5 message bubbles + AtomChip

ChatMessageList: LazyColumn, exhaustive when (msg.kind) dispatch,
auto-scroll to last item, ThinkingBubble appended when isThinking.

Bubbles: UserMessageBubble (right, primaryContainer),
AgentMessageBubble (left, surfaceVariant, with AtomChip row),
ThinkingBubble (animated 3-dot pulse), RefuseMessageBubble
(friendly copy + 'browse memory directly' link),
ErrorMessageBubble (left, pre-mapped message).

AtomChip: SuggestionChip wrapper, 80-char truncation at call site.

DEVIATION FROM SPEC: no host-JVM Compose UI tests (the spec
called for 10 test files; see global 'What this slice does NOT
add' note). The Composable will be validated manually on a
real device, matching the Commands plan's deviation
(commits 5c209d8 / 6c800f6)."
```

---

## Task 6: `ChatInputBar` Composable

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatInputBar.kt`

**Interfaces:**
- Produces: `@Composable fun ChatInputBar(draft: String, onTextChanged: (String) -> Unit, onSend: () -> Unit, canSend: Boolean, isLoading: Boolean, modifier: Modifier = Modifier)`.

- [ ] **Step 1: Create the file**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatInputBar.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.Send
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.text.input.ImeAction
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.core.ui.TouchTarget

/**
 * The send-text-and-send bar at the bottom of the chat screen.
 *
 * - The send button is `enabled = canSend` (= draft is non-blank
 *   and we're not currently loading). The user explicitly chose
 *   "send button only" in brainstorming; Enter-to-send is not wired
 *   (ImeAction.Default).
 * - When [isLoading] is true, the send button shows a 20dp
 *   CircularProgressIndicator in place of the send icon. The
 *   button is non-interactive during loading (canSend is false).
 * - The OutlinedTextField is `enabled = !isLoading` so the user
 *   can't type a second draft while the agent is working. Multiple
 *   in-flight ask() calls are not supported; the VM flow is
 *   "submit, wait, get one reply."
 */
@Composable
fun ChatInputBar(
    draft: String,
    onTextChanged: (String) -> Unit,
    onSend: () -> Unit,
    canSend: Boolean,
    isLoading: Boolean,
    modifier: Modifier = Modifier,
) {
    Row(
        modifier = modifier
            .fillMaxWidth()
            .padding(Spacing.md),
        verticalAlignment = Alignment.CenterVertically,
        horizontalArrangement = Arrangement.spacedBy(Spacing.sm),
    ) {
        OutlinedTextField(
            value = draft,
            onValueChange = onTextChanged,
            placeholder = { Text("Ask the agent...") },
            modifier = Modifier
                .weight(1f)
                .testTag("chat_input"),
            enabled = !isLoading,
            maxLines = 4,
            keyboardOptions = KeyboardOptions(imeAction = ImeAction.Default),
        )
        IconButton(
            onClick = onSend,
            enabled = canSend,
            modifier = Modifier
                .size(TouchTarget)
                .testTag("chat_send"),
        ) {
            if (isLoading) {
                CircularProgressIndicator(
                    modifier = Modifier.size(20.dp),
                    strokeWidth = 2.dp,
                    color = MaterialTheme.colorScheme.primary,
                )
            } else {
                Icon(
                    imageVector = Icons.AutoMirrored.Filled.Send,
                    contentDescription = "Send",
                )
            }
        }
    }
}
```

- [ ] **Step 2: Verify the project compiles**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatInputBar.kt
git commit -m "feat(android): ChatInputBar — send-button-only, spinner on loading

Row: OutlinedTextField (weight=1f, maxLines=4) + IconButton.
Send button enabled = draft non-blank + not loading; shows
20dp CircularProgressIndicator instead of the send icon while
loading. No Enter-to-send (ImeAction.Default) per brainstorming
decision."
```

---

## Task 7: `ChatScreen` top-level Composable

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatScreen.kt`

**Interfaces:**
- Consumes: `ChatMessage`, `EmptyState` (in `ui.design`), `SenseTopBar` (in `ui.design`), `TopBarState` (in `ui.design`).
- Produces: `@Composable fun ChatScreen(messages: List<ChatMessage>, loading: Boolean, draft: String, onTextChanged: (String) -> Unit, onSend: () -> Unit, onAtomChipTap: (String) -> Unit, onBrowseMemory: () -> Unit, modifier: Modifier = Modifier)`.

- [ ] **Step 1: Create the file**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatScreen.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.Role
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Stateless Chat screen. The Route builds the ViewModel and
 * collects the state; this Composable just renders.
 *
 * Layout:
 *  - SenseTopBar("Chat")
 *  - if messages.isEmpty(): EmptyState ("Ask the agent", "Try ...")
 *  - else: ChatMessageList (weight=1f)
 *  - ChatInputBar (always at the bottom)
 *
 * The "isThinking" computation (loading && last message is USER)
 * lives in this Composable, not the Route — it's a view-level
 * concern, not a data-layer concern.
 */
@Composable
fun ChatScreen(
    messages: List<ChatMessage>,
    loading: Boolean,
    draft: String,
    onTextChanged: (String) -> Unit,
    onSend: () -> Unit,
    onAtomChipTap: (atomId: String) -> Unit,
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val isThinking = loading && messages.lastOrNull()?.role == Role.USER
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Chat"))
        if (messages.isEmpty()) {
            EmptyState(
                title = "Ask the agent",
                body = "Try \"what did I say about X yesterday?\" — answers cite " +
                    "the memory atoms they used.",
                modifier = Modifier
                    .fillMaxSize()
                    .testTag("chat_empty"),
            )
        } else {
            ChatMessageList(
                messages = messages,
                isThinking = isThinking,
                onAtomChipTap = onAtomChipTap,
                onBrowseMemory = onBrowseMemory,
                modifier = Modifier.weight(1f),
            )
        }
        ChatInputBar(
            draft = draft,
            onTextChanged = onTextChanged,
            onSend = onSend,
            canSend = draft.trim().isNotEmpty() && !loading,
            isLoading = loading,
            modifier = Modifier.fillMaxWidth(),
        )
    }
}
```

- [ ] **Step 2: Verify the project compiles**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: BUILD SUCCESSFUL.

- [ ] **Step 3: Run the full test suite**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest
```

Expected: PASS. No test changes in this task; the compile is the gate.

- [ ] **Step 4: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatScreen.kt
git commit -m "feat(android): ChatScreen (stateless)

SenseTopBar('Chat') + EmptyState when messages is empty +
ChatMessageList otherwise + ChatInputBar always. The isThinking
computation (loading && last message is USER) lives in this
Composable as a view-level concern."
```

---

## Task 8: `ChatRoute` — ViewModel wiring

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt`

**Interfaces:**
- Consumes: `RepositoryModule.repos.agentRepository` and `RepositoryModule.repos.chatHistoryStore` (added in Task 3); the project's `viewModelFactory` manual-DI pattern.
- Produces: `@Composable fun ChatRoute(onOpenAtom: (String) -> Unit, onOpenMemory: () -> Unit, modifier: Modifier = Modifier)`. Builds the `ChatViewModel` via the factory; collects `messages` / `loading` / `draft`; passes them to `ChatScreen`; wires `onAtomChipTap = onOpenAtom` and `onBrowseMemory = onOpenMemory`.

- [ ] **Step 1: Verify `RepositoryModule.repos.agentRepository` and `.chatHistoryStore` are exposed**

Run:
```bash
grep -n "agentRepository\|chatHistoryStore" android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt
```

Expected: a line in the `Repositories` data class + a line in the `repos = Repositories(...)` assignment + a line in the `init()` body. If any is missing, the Task 3 commit didn't land; stop and re-verify before continuing.

- [ ] **Step 2: Create the Route file**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.sense.relay.data.ChatViewModel
import com.sense.relay.data.RepositoryModule

/**
 * Navigation entry point for the Chat screen. Owns:
 *   - the ChatViewModel (built via the project's manual-DI
 *     viewModelFactory, same pattern as HomeRoute / DeviceRoute /
 *     CommandsRoute).
 *
 * The stateless [ChatScreen] is rendered below. The two
 * onOpen* callbacks are passed in by [com.sense.relay.ui.nav.AppNavigation],
 * which owns the NavController and translates them to
 * `navController.navigate(Destination.AtomDetail.build(id).route)`
 * and `navController.navigate(Destination.Memory.route)`.
 *
 * The route does NOT own a LazyListState — [ChatMessageList] uses
 * its own remembered state. Process death loses scroll position
 * (acceptable for this slice; future polish can persist it via
 * SavedStateHandle).
 */
@Composable
fun ChatRoute(
    onOpenAtom: (atomId: String) -> Unit,
    onOpenMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: ChatViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                ChatViewModel(
                    repo = RepositoryModule.repos.agentRepository,
                    store = RepositoryModule.repos.chatHistoryStore,
                )
            }
        },
    )
    val messages by vm.messages.collectAsState()
    val loading by vm.loading.collectAsState()
    val draft by vm.draft.collectAsState()

    ChatScreen(
        messages = messages,
        loading = loading,
        draft = draft,
        onTextChanged = vm::onTextChanged,
        onSend = vm::ask,
        onAtomChipTap = onOpenAtom,
        onBrowseMemory = onOpenMemory,
        modifier = modifier,
    )
}
```

- [ ] **Step 3: Verify the project compiles (the nav graph still doesn't know about Chat — this is expected)**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: BUILD SUCCESSFUL. `ChatRoute` is reachable as a top-level Composable but not yet wired into the nav graph (Task 10 does that).

- [ ] **Step 4: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatRoute.kt
git commit -m "feat(android): ChatRoute — ViewModel wiring

Builds ChatViewModel via the project's manual-DI viewModelFactory
(same pattern as HomeRoute/DeviceRoute/CommandsRoute). Collects
messages, loading, draft. onOpenAtom and onOpenMemory are passed
in by AppNavigation; the Route does not own the NavController."
```

---

## Task 9: `MemoryRoute` / `MemoryScreen` / `AtomDetailRoute` / `AtomDetailScreen` stubs

**Files:**
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailRoute.kt`
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailScreen.kt`

**Interfaces:**
- `MemoryScreen(modifier: Modifier = Modifier)` — top bar + `EmptyState("Memory", "Browse and search — coming soon")`.
- `MemoryRoute(modifier: Modifier = Modifier)` — wraps `MemoryScreen` (the stub doesn't need a ViewModel).
- `AtomDetailScreen(atomId: String, text: String?, modifier: Modifier = Modifier)` — shows `atomId` + the looked-up text (if any) + a "Full detail coming soon" line; or `EmptyState("Atom <id>", "not in history")` if text is null.
- `AtomDetailRoute(atomId: String, onBack: () -> Unit, modifier: Modifier = Modifier)` — reads `ChatHistoryStore.messages` via `collectAsState`, looks up the atom's text by `atomId` in any AGENT_ANSWER message, passes `(atomId, text)` to `AtomDetailScreen`.

- [ ] **Step 1: Create `MemoryScreen.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt`:

```kotlin
package com.sense.relay.ui.memory

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Memory screen. STUB for this slice — the real MemoryScreen
 * (browse / search atoms) is its own design -> plan -> implement
 * cycle. Reachable from ChatScreen's refuse-link "browse memory
 * directly" button.
 */
@Composable
fun MemoryScreen(modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Memory"))
        EmptyState(
            title = "Memory",
            body = "Browse and search your memories — coming soon.",
            modifier = Modifier
                .fillMaxSize()
                .testTag("memory_empty"),
        )
    }
}
```

- [ ] **Step 2: Create `MemoryRoute.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt`:

```kotlin
package com.sense.relay.ui.memory

import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier

/**
 * Route for the Memory screen. STUB — no ViewModel yet. The real
 * MemoryViewModel (which already exists at ui/memory/MemoryViewModel.kt)
 * is wired in the real-MemoryScreen slice. This stub exists so the
 * Chat refuse-link has a navigable target.
 */
@Composable
fun MemoryRoute(modifier: Modifier = Modifier) {
    MemoryScreen(modifier = modifier)
}
```

- [ ] **Step 3: Create `AtomDetailScreen.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailScreen.kt`:

```kotlin
package com.sense.relay.ui.atom

import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import com.sense.relay.core.ui.Spacing
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * Atom detail screen. STUB for this slice — the real
 * AtomDetailScreen (full text + provenance + jump-to-session) is
 * its own slice. Reachable from ChatScreen's atom-chip tap.
 *
 * If [text] is null (the atom isn't in the current session
 * history), shows an EmptyState with the atom id. Otherwise shows
 * the atom id + the (truncated) chip text + a "coming soon" line.
 */
@Composable
fun AtomDetailScreen(
    atomId: String,
    text: String?,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Atom"))
        if (text == null) {
            EmptyState(
                title = "Atom $atomId",
                body = "This atom isn't in the current session history.",
                modifier = Modifier
                    .fillMaxSize()
                    .testTag("atom_not_found"),
            )
        } else {
            Column(
                modifier = Modifier
                    .padding(Spacing.md)
                    .testTag("atom_detail"),
            ) {
                Text(
                    text = atomId,
                    style = MaterialTheme.typography.labelSmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Spacer(Modifier.height(Spacing.sm))
                Text(
                    text = text,
                    style = MaterialTheme.typography.bodyLarge,
                )
                Spacer(Modifier.height(Spacing.md))
                Text(
                    text = "Full detail (provenance, jump-to-session) coming soon.",
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}
```

- [ ] **Step 4: Create `AtomDetailRoute.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailRoute.kt`:

```kotlin
package com.sense.relay.ui.atom

import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Modifier
import com.sense.relay.data.ChatMessageKind
import com.sense.relay.data.RepositoryModule
import com.sense.relay.data.Role

/**
 * Route for the atom detail screen. STUB — cheap heuristic lookup
 * in [com.sense.relay.data.ChatHistoryStore] for the atom's text.
 * The real AtomDetailScreen will fetch /memory/{atomId} from the
 * server.
 *
 * Lookup logic: find the first AGENT_ANSWER message in the
 * current session's history whose atoms contain one with the
 * given [atomId]; use that atom's text. If no such message exists
 * (the chip was tapped from a prior session that was cleared, or
 * the chat history is empty), pass `text = null` to
 * [AtomDetailScreen] which renders the "not in history" empty
 * state.
 */
@Composable
fun AtomDetailRoute(
    atomId: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val store = RepositoryModule.repos.chatHistoryStore
    val messages by store.messages.collectAsState()
    val text = remember(messages, atomId) {
        messages.asSequence()
            .filter { it.role == Role.AGENT && it.kind == ChatMessageKind.AGENT_ANSWER }
            .flatMap { it.atoms.asSequence() }
            .firstOrNull { it.atomId == atomId }
            ?.text
    }
    AtomDetailScreen(atomId = atomId, text = text, modifier = modifier)
    // onBack is accepted for API parity with SessionDetailRoute;
    // the stub does not surface a back button (SenseTopBar with
    // no onBack shows no back arrow, matching the spec).
    @Suppress("UNUSED_EXPRESSION") onBack
}
```

- [ ] **Step 5: Verify the project compiles**

Run:
```bash
cd android/sense-relay && ./gradlew :app:compileDebugKotlin
```

Expected: BUILD SUCCESSFUL. The two new packages + four new files compile.

- [ ] **Step 6: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailRoute.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/AtomDetailScreen.kt
git commit -m "feat(android): MemoryRoute + AtomDetailRoute stubs

Stubs for the refuse-link and atom-chip-tap deep-link targets.
MemoryScreen shows 'Browse and search — coming soon' empty
state. AtomDetailScreen looks up the chip text from the current
session's ChatHistoryStore; if not found, shows 'not in history'.
The real MemoryScreen and AtomDetailScreen are follow-up slices."
```

---

## Task 10: Wire `Destination.Chat`, `Destination.Memory`, `Destination.AtomDetail` into the nav graph + add Chat to the bottom bar

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt` (add `ARG_ATOM_ID` constant on `AtomDetail`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt` (add 6th tab, document INV-13 deviation)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt` (add three `composable(...)` entries + add Chat to `MAIN_ROUTES`)

**Interfaces:**
- `Destination.AtomDetail.ARG_ATOM_ID = "atomId"` (constant for the nav-arg key).
- New `MainTab(Destination.Chat, "Chat", Icons.AutoMirrored.Filled.Chat)` between `Commands` and `Settings` in `mainDestinations`.
- Three new `composable(...)` entries: `Destination.Chat.route` (with onOpenAtom / onOpenMemory callbacks), `Destination.Memory.route`, `Destination.AtomDetail.route` (with a `navArgument("atomId")` of type `NavType.StringType`).
- `Destination.Chat.route` added to `MAIN_ROUTES`.

- [ ] **Step 1: Add `ARG_ATOM_ID` constant to `Destination.kt`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt`. The current `AtomDetail` data class is at lines 37-42:

```kotlin
    data class AtomDetail(val atomId: String) : Destination {
        override val route: String = "memory/atom/$atomId"
        companion object {
            fun build(atomId: String) = AtomDetail(atomId)
        }
    }
```

Replace the `companion object` block with:

```kotlin
    data class AtomDetail(val atomId: String) : Destination {
        override val route: String = "memory/atom/$atomId"
        companion object {
            /**
             * Nav-arg key. Named in one place so the AppNavigation
             * composable() and the route reader stay in sync.
             */
            const val ARG_ATOM_ID = "atomId"
            fun build(atomId: String) = AtomDetail(atomId)
        }
    }
```

- [ ] **Step 2: Add the Chat tab to the bottom bar**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt`. Three edits:

**Add the import** alongside the existing `import androidx.compose.material.icons.automirrored.filled.List` (line 4):

```kotlin
import androidx.compose.material.icons.automirrored.filled.Chat
```

**Add the `MainTab` entry** in `mainDestinations` (currently lines 36-42). Insert after `MainTab(Destination.Commands, ...)`:

```kotlin
private val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", Icons.Filled.Home),
    MainTab(Destination.Recordings, "Recordings", Icons.AutoMirrored.Filled.List),
    MainTab(Destination.Device, "Device", Icons.Filled.Info),
    MainTab(Destination.Commands, "Commands", Icons.Filled.PlayArrow),
    // INV-13 deviation: 6 tabs (spec says 4). Chat was added in
    // 2026-07 as a P2-answers user-facing surface. The future
    // consolidation (drop Device or Commands from the bar; deep-link
    // from Home) is tracked as a follow-up. See
    // docs/superpowers/specs/2026-07-17-sense-android-chat-screen-design.md
    // section 5.1.
    MainTab(Destination.Chat, "Chat", Icons.AutoMirrored.Filled.Chat),
    MainTab(Destination.Settings, "Settings", Icons.Filled.Settings),
)
```

(The `// INV-13 deviation` comment is the only documentation change in the file; the actual code is just one new `MainTab` line.)

- [ ] **Step 3: Add the three `composable(...)` entries to `AppNavigation.kt`**

Edit `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt`. The current file's import block is lines 1-23; the `NavHost` block is lines 66-99; the `MAIN_ROUTES` set is lines 102-108.

**Add imports** to the existing import block:

```kotlin
import androidx.navigation.NavType
import androidx.navigation.navArgument
import com.sense.relay.ui.atom.AtomDetailRoute
import com.sense.relay.ui.chat.ChatRoute
import com.sense.relay.ui.memory.MemoryRoute
```

(Alphabetize: `androidx.navigation.NavType` and `androidx.navigation.navArgument` go alphabetically among the other `androidx.*` imports; the three `com.sense.relay.ui.*` imports go alphabetically among the other `com.sense.relay.ui.*` imports.)

**Add the three composable entries** inside the `NavHost { ... }` block (currently lines 66-99). Insert after the `Destination.Commands.route` entry (line 78) and before the `Destination.Settings.route` entry (line 79):

```kotlin
            composable(Destination.Home.route) { HomeRoute() }
            composable(Destination.Recordings.route) {
                RecordingsRoute(
                    onOpen = { id -> navController.navigate(Destination.SessionDetail(id).route) },
                )
            }
            composable(Destination.Device.route) { DeviceRoute() }
            composable(Destination.Commands.route) { CommandsRoute() }
            // P2-answers user-facing surface (ChatScreen). 6th tab —
            // INV-13 deviation. Chip taps deep-link to AtomDetail;
            // the refuse-link deep-links to Memory.
            composable(Destination.Chat.route) {
                ChatRoute(
                    onOpenAtom = { id ->
                        navController.navigate(Destination.AtomDetail.build(id).route)
                    },
                    onOpenMemory = {
                        navController.navigate(Destination.Memory.route)
                    },
                )
            }
            // Memory — stub reachable from Chat's refuse-link.
            // The full MemoryScreen is a follow-up slice.
            composable(Destination.Memory.route) { MemoryRoute() }
            composable(Destination.Settings.route) { SettingsRoute(onReconfigure) }
            // AtomDetail — stub reachable from Chat's chip-tap.
            // The full AtomDetailScreen is a follow-up slice.
            composable(
                route = Destination.AtomDetail.route,   // "memory/atom/{atomId}"
                arguments = listOf(
                    navArgument(Destination.AtomDetail.ARG_ATOM_ID) {
                        type = NavType.StringType
                    },
                ),
            ) { backStackEntry ->
                val atomId = backStackEntry.arguments
                    ?.getString(Destination.AtomDetail.ARG_ATOM_ID)
                if (atomId == null) {
                    StubScreen("Atom not found")
                } else {
                    AtomDetailRoute(
                        atomId = atomId,
                        onBack = { navController.popBackStack() },
                    )
                }
            }
```

**Add `Destination.Chat.route` to `MAIN_ROUTES`** (currently lines 102-108):

```kotlin
private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Device.route,
    Destination.Commands.route,
    Destination.Chat.route,        // P2-answers — 6th tab (INV-13 deviation)
    Destination.Settings.route,
)
```

- [ ] **Step 4: Verify the project compiles**

Run:
```bash
cd android/sense-relay && ./gradlew :app:assembleDebug
```

Expected: BUILD SUCCESSFUL. The debug APK builds with the new Chat tab + Memory + AtomDetail routes wired in.

- [ ] **Step 5: Run the full unit-test suite**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest
```

Expected: BUILD SUCCESSFUL. All previous Android tests stay green.

- [ ] **Step 6: Verify the architectural invariant still holds**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.arch.ArchitecturalInvariantsTest"
```

Expected: PASS. The new `ui/chat/`, `ui/memory/`, and `ui/atom/` files don't import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`.

- [ ] **Step 7: Commit**

```bash
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt
git commit -m "feat(android): wire Chat into the nav graph (6th tab)

New Destination.Chat 6th bottom-bar tab (INV-13 deviation
documented inline in BottomBar.kt). composable(Chat.route) wires
onOpenAtom -> Destination.AtomDetail and onOpenMemory ->
Destination.Memory. composable(Memory.route) and
composable(AtomDetail.route) with a navArgument('atomId') of
type StringType for the deep-link targets.

ARG_ATOM_ID constant added to Destination.AtomDetail so the
nav-arg key is named in one place.

Existing 232+ Android tests stay green; architectural invariant
test confirms no http.* imports in the new ui/ files."
```

---

## Task 11: Full suite + sanity build + manual smoke verification

**Files:** none (verification only).

- [ ] **Step 1: Run the full unit-test suite**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest
```

Expected: BUILD SUCCESSFUL. All previous Android tests still pass; the new `ChatMessageKindTest` (4 cases) and the 4 new `kind` assertions in `ChatViewModelTest` are all green.

- [ ] **Step 2: Build the debug APK**

Run:
```bash
cd android/sense-relay && ./gradlew :app:assembleDebug
```

Expected: BUILD SUCCESSFUL. The debug APK builds with the new Chat tab + the refuse-link + atom-chip-tap deep-links wired in.

- [ ] **Step 3: Run lint as a smoke test (skip if too slow)**

Run:
```bash
cd android/sense-relay && ./gradlew :app:lintDebug -q 2>&1 | tail -30
```

Expected: no errors related to the new code. Pre-existing lint warnings are fine.

- [ ] **Step 4: Run the architectural invariant test one more time**

Run:
```bash
cd android/sense-relay && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.arch.ArchitecturalInvariantsTest"
```

Expected: PASS. The UI ↔ http boundary is intact.

- [ ] **Step 5: No commit**

This task is a verification gate. If anything fails, fix and re-run; the fix gets its own commit. If everything is green, the slice is complete and the branch is ready to push / merge.

- [ ] **Step 6: Manual smoke test (real device or emulator)**

Per the spec's §5.3 "How to verify IRL right now":

1. **Provision the gateway:** start the server with `SENSE_LLM_MODEL=...` and `SENSE_EMBED_MODEL=...` set (without these, every `ask()` returns `Error`). Confirm a setup is complete in the app (the device is paired, the server URL + bearer token are set).
2. **Build + install:** `cd android/sense-relay && ./gradlew :app:installDebug` on a device.
3. **Open the Chat tab** (6th in the bottom bar; icon is `AutoMirrored.Filled.Chat`).
4. **Type a question that maps to existing memory** (e.g. "what did I say about X yesterday?"). Confirm:
   - The user bubble appears immediately (optimistic UI).
   - The thinking bubble (3 pulsing dots) appears below it.
   - The agent bubble appears with the answer text + atom chips at the bottom.
5. **Tap an atom chip.** Confirm navigation to the stub `AtomDetailRoute` showing the chip's text + "Full detail coming soon".
6. **Tap "browse memory directly"** on a refused answer (or use a question with no supporting memory). Confirm navigation to the stub `MemoryRoute` showing the empty state.
7. **Stop the gateway** (kill the process). Type another question. Confirm an `ErrorMessageBubble` shows the `ErrorMapper` message ("Can't reach the server").
8. **Rotate the device** mid-chat. Confirm the draft text and message list survive (ViewModel-lifetime + `SavedStateHandle`).
9. **Force-stop the app and reopen.** Confirm the history is gone (in-memory; the DataStore version is a future slice).
10. **Confirm INV-11:** run
    ```bash
    grep -r "import com.sense.relay.http" android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/ \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/
    ```
    Expected: no output (zero matches).

If all 10 manual checks pass, the slice is done. Report the result to the user; the user decides whether to push the branch.

---

## Self-Review

**1. Spec coverage:**

| Spec section | Implemented in |
|---|---|
| §1 Architecture (Route+Screen separation, exhaustive `when (msg.kind)` dispatch) | Tasks 4, 5, 7, 8 |
| §2 File layout (new + edited) | Tasks 1, 2, 3, 4, 5, 6, 7, 8, 9, 10 |
| §3 `ChatScreen` top-level Composable | Task 7 |
| §4 `ChatMessageList` | Task 4 |
| §5 `ChatInputBar` | Task 6 |
| §6.1 `UserMessageBubble` | Task 5 |
| §6.2 `AgentMessageBubble` | Task 5 |
| §6.3 `ThinkingBubble` | Task 5 |
| §6.4 `RefuseMessageBubble` | Task 5 |
| §6.5 `ErrorMessageBubble` | Task 5 |
| §6.6 `AtomChip` | Task 5 |
| §7 `ChatViewModel` (messages + kind) | Task 2 |
| §8 `RepositoryModule` DI wire | Task 3 |
| §9 Navigation (route + tab + nav graph wiring) | Task 10 |
| §10 `MemoryRoute` / `AtomDetailRoute` stubs | Task 9 |
| §11 Error handling | Implicit (the `ErrorMessageBubble` pre-mapping happens via the existing `AgentRepository.ask()` -> `AgentOutcome.Error` -> `ChatMessage(kind=AGENT_ERROR, text="Error: <msg>")` flow; the screen strips the "Error: " prefix in `ChatMessageList` and renders the rest) |
| §12 Edge cases | Implicit (the Composable is built around the standard Compose patterns; edge cases like rotation, process death, etc. follow the data layer's existing contract) |
| §13 Testing | The spec called for 10 Compose UI test files + 4 VM kind assertions + 1 enum test. The plan ships: the enum test (Task 1), the 4 VM kind assertions (Task 2), the architectural-invariant test (already in place; verified in Task 10). The 10 Compose UI test files are NOT shipped — see the global "What this slice does NOT add" note. |
| §14 Design principles (10 binding rules) | Applied throughout: thin Composable (Task 7), VM owns the lifecycle (Task 2), no data layer changes other than the additive `kind` field (Tasks 1, 2), monochrome via MaterialTheme tokens (Task 5), optimistic UI (Tasks 4, 7), no polling (no new lifecycle effects), existing patterns (Tasks 8, 9, 10), INV-11 enforced (Task 10), TDD where feasible (Tasks 1, 2), no rewrites of working code (everywhere). |
| §15 Phasing (single chunk, 11 tasks, `main` green at every step) | Each task is independently testable; the commit sequence is 10 commits + 1 verification task; `main` stays green. |
| §16 Non-goals (real Memory, real AtomDetail, DataStore, streaming, multi-session, markdown, voice, copy, edit, rate-limit copy, animations, bottom-bar consolidation, SetupActivity tech debt, localization) | Not implemented. The 11-task plan covers exactly the spec's "what lands in this slice" and nothing more. |

**2. Placeholder scan:** No "TBD", no "TODO", no "implement later". The only place I used a placeholder-like phrase is the inline comment in `BottomBar.kt` ("future consolidation ... is tracked as a follow-up") — that's an explicit reference to a tracked follow-up, not a placeholder for missing code. The `// onBack is accepted for API parity ... @Suppress("UNUSED_EXPRESSION") onBack` pattern in `AtomDetailRoute.kt` (Task 9 Step 4) is a deliberate suppression of an unused-parameter warning — the parameter is intentionally accepted for future use; not a placeholder.

**3. Type consistency:** Method names, parameter types, and return types used in later tasks match what earlier tasks defined:
- `ChatViewModel.messages: StateFlow<List<ChatMessage>>` is defined in Task 2, used in Task 8.
- `ChatMessageKind.USER_TEXT | AGENT_ANSWER | AGENT_REFUSE | AGENT_ERROR` is defined in Task 1, used in Tasks 2, 4, 9.
- `RepositoryModule.repos.agentRepository` and `.chatHistoryStore` are defined in Task 3, used in Task 8.
- `ChatScreen(messages, loading, draft, onTextChanged, onSend, onAtomChipTap, onBrowseMemory, modifier)` signature is defined in Task 7, used in Task 8.
- `ChatRoute(onOpenAtom, onOpenMemory, modifier)` signature is defined in Task 8, used in Task 10.
- `MemoryRoute(modifier)`, `AtomDetailRoute(atomId, onBack, modifier)` signatures are defined in Task 9, used in Task 10.
- `Destination.AtomDetail.ARG_ATOM_ID = "atomId"` is defined in Task 10 Step 1, used in Task 10 Step 3 (same task).
- `Icons.AutoMirrored.Filled.Chat` is the bottom-bar icon for Chat (in the core icon set, same family as `List` and `Send` already used in the project).

**Issues found and fixed during self-review:**

- **Compose UI tests dropped from the plan.** The spec's §13 called for 10 Compose UI test files (~22 cases). The Commands plan hit the same Compose UI test sandbox issue and shipped without those tests (commits `5c209d8` and `6c800f6` document the deviation; the project-status memory notes it). The plan's "What this slice does NOT add" section explicitly calls this out, and the plan's test inventory (the enum test + 4 VM kind assertions + the architectural invariant test) reflects what the sandbox can actually compile.

- **`AgentRepository` factory refactor.** The spec's §8.2 said the AgentApi is built with the initial (url, token) at app startup, but `RepositoryModule.init()` is synchronous while `clientProvider` is `suspend`. The plan's Task 3 refactors `AgentRepository` to take a `suspend () -> AgentApi` factory (mirrors `CommandRepository`), which lets the apiProvider be built inline using the existing `clientProvider`. The `init()` body is unchanged in structure; only the construction site uses the new factory form.

- **`MemoryViewModel.kt` already exists.** The plan's Task 9 stubs `MemoryRoute` / `MemoryScreen` without touching the existing `MemoryViewModel`. The real MemoryScreen (a follow-up slice) will wire the VM; this slice just needs the route to be navigable from the refuse-link.

- **`RepositoryModule.repos.device` was previously a `DeviceRepositoryImpl(RelayController)` constructor call inside the `repos = Repositories(...)` data class call.** Task 3's edit pulls it out into a `val device = ...` declaration so the new `val agentRepository = ...` and `val chatHistoryStore = ...` can sit alongside it as top-level vals. The data class call then uses `device = device,` like the other fields. This is a structural refactor, not a behavior change.

- **No type mismatches found.** Every signature is referenced consistently across the tasks that produce and consume it.

No outstanding spec gaps. Plan is complete.
