# Sense — Android ChatScreen Composable Design

> **Scope:** This spec covers the single missing user-facing surface for P2-answers: the Android `ChatScreen` Composable + the `ChatRoute` that wires it + the small DI / data-layer changes needed to make the ViewModel constructible. Everything else in P2-answers (server `POST /agent`, `AgentRepository`, `AgentApi`, `ChatViewModel`, `ChatHistoryStore`) is already shipped on `main` and out of scope here.
>
> **What lands in this slice:** the Composable surface for chat. The `Memory` and `AtomDetail` routes land as stubs (reachable from the refuse-link and atom-chip-tap) — the real MemoryScreen and AtomDetailScreen are follow-up slices.

**Goal:** Close the P2-answers loop end-to-end on the user-facing side. The data layer is fully built, tested, and wired (7 `ChatViewModelTest` cases + `AgentRepositoryTest` + 232+ Android tests green). The only piece left between the user and the cognitive read path is a Compose screen that lets them ask a question, see the agent's answer with cited atom chips, and recover from errors.

**Status going in (already on `main`, working):**
- **Server:** `POST /agent` route, `Planner`, `Retriever`, `Validator`, `Guardrails`, `AuditLogger`, `MetricsRecorder`. Returns `Return` / `ReturnWithUncertainty` / `Refuse` with full provenance.
- **Android:** `AgentApi` (HTTP wrapper), `AgentRepository` (the only importer of `HttpApiError` for the read path — INV-11), `ChatViewModel` (sealed outcomes mapped to `ChatMessage` via `Role.USER` / `Role.AGENT`), `ChatHistoryStore` (in-memory, `flush()` seam for the future DataStore version), `AgentOutcome` sealed class (`Answer` / `Refuse` / `Error`), `AtomChip` data class (with `text` truncated to `MAX_ATOM_CHIP_TEXT_LEN = 240` server-side), 7 unit tests in `ChatViewModelTest`. 232+ Android tests green.
- **What's missing:** the `ChatScreen` Composable that renders the message list + input bar, the `ChatRoute` that owns the ViewModel + navigation, the `Destination.Chat` wiring into the nav graph, and a small DI wire so `RepositoryModule.repos` exposes `agentRepository` and `chatHistoryStore`.

**Tech stack:** Kotlin 1.9+ / Jetpack Compose / Hilt-free manual DI (`viewModelFactory { initializer { ... } }`, same as `HomeRoute` / `DeviceRoute` / `CommandsRoute`). No new dependencies — Material 3 `AssistChip`, `OutlinedTextField`, `IconButton`, `rememberInfiniteTransition` are all already on the classpath via `compose-bom`.

---

## 1. Architecture

The change is **Android-only**. The data layer is modified in exactly one place: a new `kind: ChatMessageKind` field on `ChatMessage` (so the screen can do exhaustive dispatch on the variant). No server changes, no firmware changes, no protocol changes, no new dependencies.

```
┌────────────────────────────────────────────────────────────────┐
│ ChatRoute (stateful)                                           │
│   ├── builds ChatViewModel via viewModelFactory                │
│   │     (RepositoryModule.repos.agentRepository,               │
│   │      RepositoryModule.repos.chatHistoryStore)              │
│   ├── collects: messages (StateFlow<List<ChatMessage>>),       │
│   │             loading (StateFlow<Boolean>),                  │
│   │             draft (StateFlow<String>)                      │
│   ├── remembers LazyListState (survives recomposition)        │
│   └── renders ChatScreen with callbacks                        │
│         ├── onTextChanged = vm::onTextChanged                  │
│         ├── onSend = vm::ask                                   │
│         ├── onAtomChipTap = onOpenAtom (from nav caller)       │
│         └── onBrowseMemory = onOpenMemory (from nav caller)    │
│                                                                │
│ ChatScreen (stateless)                                         │
│   ├── SenseTopBar("Chat")                                      │
│   ├── when (messages.isEmpty()) {                              │
│   │     Empty → EmptyState (no messages yet)                   │
│   │   } else {                                                 │
│   │     ChatMessageList(messages, isThinking, ...)             │
│   │   }                                                        │
│   └── ChatInputBar(draft, onTextChanged, onSend,               │
│                     canSend, isLoading)                        │
│                                                                │
│ ChatMessageList (stateless)                                    │
│   ├── LazyColumn (key = message.id)                            │
│   ├── LaunchedEffect(messages.size, loading) -> scrollToBottom │
│   └── per-message dispatch (exhaustive when over kind):        │
│         USER_TEXT      -> UserMessageBubble                    │
│         AGENT_ANSWER   -> AgentMessageBubble (with atoms)      │
│         AGENT_REFUSE   -> RefuseMessageBubble (Browse memory)  │
│         AGENT_ERROR    -> ErrorMessageBubble                   │
│   └── if (isThinking) -> item { ThinkingBubble } appended      │
│                                                                │
│ Bubbles (each stateless, one Composable per variant):          │
│   UserMessageBubble / AgentMessageBubble / ThinkingBubble /    │
│   RefuseMessageBubble / ErrorMessageBubble / AtomChip          │
└────────────────────────────────────────────────────────────────┘
```

**Layering (binding, matches the project's existing discipline):**

1. `ui/chat/` is the only package that defines Composables for this surface.
2. Composables import domain types from `data/` (`ChatMessage`, `Role`, `AtomChip`, `ChatMessageKind`) and the design system from `ui/design/`. No `ui/chat/` file imports `com.sense.relay.http.*` or `com.sense.relay.http.dto.*` — the existing `ArchitecturalInvariantsTest` (file-tree scan of `ui/`) guards this.
3. The Composable never sees `AgentOutcome` directly — only `ChatMessage` (the VM does the outcome-to-message mapping in `ask()`).
4. The `ChatViewModel` already exists; this slice adds exactly two small things to it:
   - `val messages: StateFlow<List<ChatMessage>> = store.messages` (passthrough)
   - `kind = ChatMessageKind.AGENT_ANSWER` / `AGENT_REFUSE` / `AGENT_ERROR` set in each `ask()` branch
5. `AgentRepository.ask()` is the only data-layer seam. INV-11 holds.

**The one new architectural decision in this slice:** `ChatMessageKind` is added as a new field on `ChatMessage` (small data-layer edit). The screen's bubble dispatch is an exhaustive `when (msg.kind)`, which makes a future "new variant" a compile error — the right failure mode. The alternative (string-prefix heuristic) is brittle and was rejected (see Section 3.4 of the brainstorm notes).

**What the slice explicitly does NOT change:** `AgentApi`, `AgentRepository`, `ChatHistoryStore` class body, `HttpApiError` mapping, `ErrorCode`, the DTOs in `http/dto/`. Server-side code. Firmware. The Commands slice's files. The setup / onboarding / device / recordings / home screens.

---

## 2. File layout

### New files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/
  ChatRoute.kt                — navigation entry: builds ChatViewModel, collects state,
                                routes chip taps to AtomDetail, refuse-link to Memory
  ChatScreen.kt               — top-level stateless Composable (SenseTopBar + list + input bar)
  ChatMessageList.kt          — LazyColumn; per-message dispatch to bubble Composables;
                                auto-scrolls to bottom on new messages / loading=true
  ChatInputBar.kt             — Row: OutlinedTextField (weight=1f) + Send IconButton
  UserMessageBubble.kt        — single bubble, right-aligned, surface tint
  AgentMessageBubble.kt       — single bubble, left-aligned, with atom chip row at the bottom
  ThinkingBubble.kt           — animated 3-dot pulse, left-aligned
  RefuseMessageBubble.kt      — agent bubble variant for Refuse outcome;
                                friendly copy + "Browse memory" text-button
  ErrorMessageBubble.kt       — agent bubble variant for Error outcome;
                                message pre-mapped via ErrorMapper in the route
  AtomChip.kt                 — small Composable for one cited atom; outlined pill,
                                tap fires onAtomChipTap(atomId)

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/
  MemoryRoute.kt              — stub: EmptyState("Memory", "Browse and search your
                                memories — coming soon") with onBack if pushed
  MemoryScreen.kt             — stateless wrapper around EmptyState for testability

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/atom/
  AtomDetailRoute.kt          — stub: takes atomId, looks up the chip text from
                                the most-recent agent message in ChatHistoryStore
                                (cheap heuristic — the real AtomDetail will fetch
                                /memory/{atomId}); EmptyState-style placeholder
  AtomDetailScreen.kt         — stateless Composable: shows atomId, kind, text,
                                "Full detail coming soon"

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/
  ChatScreenTest.kt           — Compose UI test: empty state, user-only list,
                                user+agent list, loading-think placeholder,
                                refuse bubble, error bubble, atom chip tap
  ChatInputBarTest.kt         — Compose UI test: text change, send click,
                                send disabled when blank/loading
  ChatMessageListTest.kt      — Compose UI test: scroll-to-bottom, per-variant
                                dispatch, key stability
  UserMessageBubbleTest.kt    — text rendered; right-alignment testTag present
  AgentMessageBubbleTest.kt   — text + atom chips rendered; tap forwards callback
  ThinkingBubbleTest.kt       — renders 3 dots; survives recomposition
  RefuseMessageBubbleTest.kt  — friendly copy + "Browse memory" link tap forwards
  ErrorMessageBubbleTest.kt   — message rendered verbatim (no double-mapping)
  AtomChipTest.kt             — truncation helper; tap forwards
  ChatMessageKindTest.kt      — unit test: 4 cases, one per enum value
```

### Edited files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt
                              — add `kind: ChatMessageKind = ChatMessageKind.USER_TEXT`
                                field on ChatMessage (default keeps existing
                                construction sites compiling); add the
                                ChatMessageKind sealed enum to the same file

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatViewModel.kt
                              — add `val messages: StateFlow<List<ChatMessage>> = store.messages`
                                (passthrough, the only new public surface)
                              — set `kind = ChatMessageKind.AGENT_ANSWER` /
                                `AGENT_REFUSE` / `AGENT_ERROR` in each ask() branch

android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt
                              — construct AgentRepository(AgentApi) in init() using
                                the shared clientProvider (same pattern as
                                CommandRepository); expose a process-singleton
                                ChatHistoryStore; add `agentRepository` and
                                `chatHistoryStore` to the Repositories data class
                                and to the `repos` assignment

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/AppNavigation.kt
                              — add composable(Destination.Chat.route) { ChatRoute(
                                  onOpenAtom = { id -> navController.navigate(
                                      Destination.AtomDetail.build(id).route) },
                                  onOpenMemory = { navController.navigate(
                                      Destination.Memory.route) }) }
                              — add composable(Destination.Memory.route) { MemoryRoute() }
                              — add composable(Destination.AtomDetail.route) with
                                a navArgument("atomId") { type = NavType.StringType }
                              — add Destination.Chat.route to MAIN_ROUTES

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/BottomBar.kt
                              — add MainTab(Destination.Chat, "Chat",
                                Icons.AutoMirrored.Filled.Chat) between
                                Commands and Settings (6th tab — INV-13 deviation,
                                documented inline)

android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/nav/Destination.kt
                              — add `val ARG_ATOM_ID = "atomId"` companion-object
                                constant on Destination.AtomDetail so the
                                nav-arg key is named in one place

android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ChatViewModelTest.kt
                              — add 4 one-line `kind` assertions to the existing
                                ask tests (one per outcome branch)
```

### Files explicitly NOT changed

- `AgentApi.kt`, `AgentRepository.kt`, the DTOs in `http/dto/`, `HttpApiError.kt`, `ErrorCode.kt` — INV-11 holds.
- `ChatHistoryStore` class body — only the `ChatMessage` data class gains the `kind` field; the `append` / `replace` / `clear` / `flush` API is untouched.
- `CommandApi.kt`, `CommandRepository.kt`, `CommandsViewModel.kt`, the Commands screen files.
- `build.gradle.kts` — no new dependencies.
- The setup / onboarding / device / recordings / home screens.
- The 4 existing `Destination` test files (the 6-tab deviation is documented in `BottomBar.kt` but the destination data class itself is unchanged beyond the `ARG_ATOM_ID` constant).

---

## 3. `ChatScreen` — top-level Composable

Single top-level Composable that branches on `messages.isEmpty()`. The optimistic-UI mechanics (the `isThinking` flag) are computed in the parent `ChatRoute` and passed down.

```kotlin
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
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Chat"))
        if (messages.isEmpty()) {
            EmptyState(
                title = "Ask the agent",
                body = "Try \"what did I say about X yesterday?\" — answers cite " +
                       "the memory atoms they used.",
                modifier = Modifier.fillMaxSize().testTag("chat_empty"),
            )
        } else {
            ChatMessageList(
                messages = messages,
                isThinking = loading && messages.last().role == Role.USER,
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

**Branches:**

| State | Rendered |
|-------|----------|
| `messages` empty | `EmptyState("Ask the agent", "Try ...")`. No list, no scroll. |
| `messages` non-empty, `loading=false` | `ChatMessageList` with the message bubbles. No thinking bubble. |
| `messages` non-empty, `loading=true`, last is USER | `ChatMessageList` + appended `ThinkingBubble`. |
| `messages` non-empty, `loading=true`, last is AGENT | `ChatMessageList` only (no thinking — we already have the agent's reply). |

**Layout per [[android-ui-style]] — modern, simple, monochrome, super-fluid:**

```
┌──────────────────────────────────────────────────────┐
│  Chat                                       (header)  │
│                                                      │
│  ┌────────────────────────────────────────────────┐  │
│  │ what did I say about X yesterday?       ◯ me   │  │   ← UserMessageBubble (right)
│  └────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────┐  │
│  │ You mentioned X in yesterday's morning         │  │   ← AgentMessageBubble (left)
│  │ standup: "I want to ship the chat screen        │  │
│  │ this week."                                     │  │
│  │ ◯ atom 1  ◯ atom 2                             │  │   ← tappable AtomChips
│  └────────────────────────────────────────────────┘  │
│  ┌────────────────────────────────────────────────┐  │
│  │  ●  ●  ●                                       │  │   ← ThinkingBubble (while loading)
│  └────────────────────────────────────────────────┘  │
│                                                      │
│  ┌──────────────────────────────────────┐  ┌──────┐  │
│  │ Ask the agent...                     │  │  ▶   │  │   ← ChatInputBar
│  └──────────────────────────────────────┘  └──────┘  │
└──────────────────────────────────────────────────────┘

Empty state (no messages yet):

┌──────────────────────────────────────────────────────┐
│  Chat                                                │
│                                                      │
│                Ask the agent                         │
│                                                      │
│   Try "what did I say about X yesterday?" —         │
│   answers cite the memory atoms they used.           │
│                                                      │
│  ┌──────────────────────────────────────┐  ┌──────┐  │
│  │ Ask the agent...                     │  │  ▶   │  │
│  └──────────────────────────────────────┘  └──────┘  │
└──────────────────────────────────────────────────────┘

Refuse bubble (when last agent message is AGENT_REFUSE):

┌──────────────────────────────────────────────────────┐
│  I don't have a memory about that yet.               │
│  Try asking after the wearable has captured more,    │
│  or [Browse memory]                                  │
└──────────────────────────────────────────────────────┘

Error bubble (when last agent message is AGENT_ERROR):

┌──────────────────────────────────────────────────────┐
│  Can't reach the server                              │
│  (message from ErrorMapper.toDisplayMessage)         │
└──────────────────────────────────────────────────────┘
```

---

## 4. `ChatMessageList` — the dispatch

```kotlin
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
                ChatMessageKind.USER_TEXT    -> UserMessageBubble(text = msg.text)
                ChatMessageKind.AGENT_ANSWER -> AgentMessageBubble(
                    text = msg.text,
                    atoms = msg.atoms,
                    onAtomChipTap = onAtomChipTap,
                )
                ChatMessageKind.AGENT_REFUSE -> RefuseMessageBubble(
                    onBrowseMemory = onBrowseMemory,
                )
                ChatMessageKind.AGENT_ERROR  -> ErrorMessageBubble(
                    message = msg.text.removePrefix("Error: "),
                )
            }
        }
        if (isThinking) {
            item(key = "thinking") { ThinkingBubble() }
        }
    }
}
```

**Exhaustive `when` over `msg.kind`** — the compiler enforces that a new `ChatMessageKind` is handled here. This is the right failure mode: a future "rate-limited" variant, a future "clarifying question" variant, etc. would be a compile error in this file rather than a silently-routed bubble.

**Why the `kind` enum (not string-prefix heuristic):** the data layer is `ChatMessageKind.USER_TEXT | AGENT_ANSWER | AGENT_REFUSE | AGENT_ERROR`. Adding the field is one line per `ask()` branch (4 total) and gives us exhaustive dispatch + a testable test surface. The alternative (text.startsWith("Error:") / text.startsWith("No supporting")) is brittle and survives only as long as the agent never changes its refusal phrasing.

**`key = it.id`** — message ids are unique (`msg-{ts}-{rand}` for user, `msg-{ts}-{suffix}` for agent), so reordering or recomposition does not re-bubble the same content.

---

## 5. `ChatInputBar`

```kotlin
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
            modifier = Modifier.weight(1f).testTag("chat_input"),
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

**Properties:**
- Send button is `enabled = canSend` (= `draft.trim().isNotEmpty() && !loading`). The blank-draft case is handled at the input-bar level, not by a `ViewModel.ask()` early-return.
- The send button shows a 20dp spinner in place of the send icon when `isLoading=true` (not disabled — the disabled state would be ambiguous; the spinner shows the agent is working).
- The `OutlinedTextField` is `enabled = !isLoading` so the user can't type while the agent is thinking. (Multiple in-flight `ask()` calls are not supported; the `ChatViewModel.ask()` flow is "submit, wait, get one reply.")
- `maxLines = 4` so the input doesn't grow unboundedly on long drafts.
- No Enter-to-send (`ImeAction.Default`) — the user explicitly chose Send-button-only in brainstorming. The visible send button is the affordance.

---

## 6. Bubbles — the variant set

### 6.1 — `UserMessageBubble`

Right-aligned bubble. Surface tint (subtle background to distinguish from the agent bubbles). Text in `bodyLarge`. Touch target: row is tappable for future "edit / re-ask" features (out of scope, but the layout doesn't preclude it).

```kotlin
@Composable
fun UserMessageBubble(text: String, modifier: Modifier = Modifier) {
    Row(modifier = modifier.fillMaxWidth(), horizontalArrangement = Arrangement.End) {
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

### 6.2 — `AgentMessageBubble`

Left-aligned bubble. Default surface. Text in `bodyLarge`. If `atoms` is non-empty, a `Row` of `AtomChip` composables below the text (single line, horizontally scrollable inside the bubble, no wrapping).

```kotlin
@Composable
fun AgentMessageBubble(
    text: String,
    atoms: List<AtomChip>,
    onAtomChipTap: (String) -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(modifier = modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
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
                    Row(
                        horizontalArrangement = Arrangement.spacedBy(Spacing.xs),
                    ) {
                        atoms.forEach { atom ->
                            AtomChip(
                                text = atom.text.truncate(80),
                                onClick = { onAtomChipTap(atom.atomId) },
                            )
                        }
                    }
                }
            }
        }
    }
}
```

**Why truncate chip text to 80 chars:** the server already truncates atom text to `MAX_ATOM_CHIP_TEXT_LEN = 240`. The 80-char cap here is a defensive UI cap so a single chip doesn't dominate the row. The "…" suffix is appended if the original is longer than 80 chars. The full text is still on the server; the chip is a label, not the citation.

### 6.3 — `ThinkingBubble`

Three small dots, animated via `rememberInfiniteTransition`. Each dot pulses opacity (0.3 → 1.0 → 0.3) with a 150ms phase offset between dots, total cycle 1.2s. Left-aligned, same surface as the agent bubble but with a smaller height (no padding, just the dots).

```kotlin
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
            Box(
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

### 6.4 — `RefuseMessageBubble`

Left-aligned. Friendly copy: "I don't have a memory about that yet. Try asking after the wearable has captured more, or browse memory directly." The "browse memory directly" portion is a `TextButton` that fires `onBrowseMemory`.

```kotlin
@Composable
fun RefuseMessageBubble(
    onBrowseMemory: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Row(modifier = modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
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
                    "I don't have a memory about that yet. Try asking after " +
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

### 6.5 — `ErrorMessageBubble`

Left-aligned. Renders the pre-mapped message from `ErrorMapper.toDisplayMessage(ApiError)`. The route is responsible for the mapping (it has access to `HttpApiError`/`ApiError`); the screen receives a clean `String`.

```kotlin
@Composable
fun ErrorMessageBubble(
    message: String,
    modifier: Modifier = Modifier,
) {
    Row(modifier = modifier.fillMaxWidth(), horizontalArrangement = Arrangement.Start) {
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

**The route does the mapping** — the bubble receives a `String`. This keeps the bubble ignorant of the error code path, matches the pattern in `HomeScreen` (which receives a `HomeUiState.Failed(reason)` already-mapped string), and means the test for the bubble is one assertion: "the message string is rendered verbatim."

### 6.6 — `AtomChip`

A small outlined pill. Uses Material 3 `SuggestionChip` (the closest primitive to "a small tappable label"). The text is truncated at the call site (`AgentMessageBubble` does the 80-char cap).

```kotlin
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
            )
        },
        modifier = modifier.testTag("atom_chip"),
    )
}
```

---

## 7. `ChatViewModel` — the two changes

The VM gains exactly two things. Both are additive; the existing 7 tests continue to pass.

### 7.1 — `messages` passthrough

```kotlin
class ChatViewModel(
    private val repo: AgentRepository,
    private val store: ChatHistoryStore,
    private val sessionId: String? = null,
    savedState: SavedStateHandle = SavedStateHandle(),
) : ViewModel() {

    // NEW: passthrough for the Composable to collect.
    val messages: StateFlow<List<ChatMessage>> = store.messages

    // existing: val draft, val loading, init { ... }, onTextChanged, ask, clear
    // (unchanged)
}
```

`ChatHistoryStore.messages` is already a `StateFlow<List<ChatMessage>>`, so this is a one-line passthrough. No new behavior, no new state.

### 7.2 — `kind` assignment in `ask()`

In each of the three `AgentOutcome` branches in `ask()`, set `kind = ChatMessageKind.AGENT_*` on the appended `ChatMessage`. Four one-line changes:

```kotlin
// Answer branch:
val agent = ChatMessage(
    id = "msg-${System.currentTimeMillis()}-agent",
    role = Role.AGENT,
    kind = ChatMessageKind.AGENT_ANSWER,    // NEW
    text = outcome.text,
    atoms = outcome.atoms.map { ... },
    traceRequestId = outcome.trace.requestId,
    ...
)

// Refuse branch:
val msg = ChatMessage(
    id = "msg-${System.currentTimeMillis()}-refuse",
    role = Role.AGENT,
    kind = ChatMessageKind.AGENT_REFUSE,    // NEW
    text = outcome.reason.replace(...),
    ...
)

// Error branch:
val msg = ChatMessage(
    id = "msg-${System.currentTimeMillis()}-error",
    role = Role.AGENT,
    kind = ChatMessageKind.AGENT_ERROR,     // NEW
    text = "Error: ${outcome.message}",
    ...
)
```

The user-typed message in `ask()` is the only one that needs no `kind` because it defaults to `ChatMessageKind.USER_TEXT` (the new field's default value).

### 7.3 — `ChatMessage` and `ChatMessageKind`

In `ChatHistoryStore.kt` (same file as `ChatMessage`):

```kotlin
enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR }

data class ChatMessage(
    val id: String,
    val role: Role,
    val kind: ChatMessageKind = ChatMessageKind.USER_TEXT,  // NEW; default keeps
                                                              // existing construction
                                                              // sites compiling
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
)
```

**Why default the `kind` to `USER_TEXT`:** every existing construction of a `ChatMessage` (the 7 tests + the user-message branch in `ask()`) doesn't set `kind`. The default keeps them compiling and semantically correct (a user-typed message is `USER_TEXT` by definition). The 3 agent branches in `ask()` get the explicit assignment.

---

## 8. `RepositoryModule` — the one new DI wire

### 8.1 — Imports

Add to `RepositoryModule.kt`:

```kotlin
import com.sense.relay.data.AgentApi            // already exists
import com.sense.relay.data.AgentRepository     // already exists
import com.sense.relay.data.ChatHistoryStore    // already exists
```

### 8.2 — `init(app)` additions

After the existing `commandRepository` construction in `init(app)`:

```kotlin
// Chat (read path). The AgentApi is created on demand from the
// shared clientProvider so a re-provision takes effect on the next
// /agent call without rebuilding the repository — same pattern as
// CommandRepository's apiProvider.
val agentRepository = AgentRepository(
    api = AgentApi(
        baseUrl = clientProvider().baseUrl,
        token = clientProvider().token,
    ),
)
// ChatHistoryStore is process-singleton (not per-ViewModel) so a
// deep-link hop (Chat -> AtomDetail -> back) doesn't lose history.
// The future DataStore-backed version keeps the same accessor and
// persists across process restarts.
val chatHistoryStore = ChatHistoryStore()
```

**Note on `clientProvider()`:** the existing `clientProvider` returns a `suspend () -> SenseHttpClient`, so the two `.baseUrl` / `.token` calls happen inside a coroutine. The construction site is in `init()`, which is called from `SenseApplication.onCreate()` — a synchronous Android lifecycle hook. The cleanest approach: build the apiProvider as a factory `() -> AgentApi` that closes over `clientProvider`, and pass it to the `AgentRepository`. The `AgentRepository(api = ...)` constructor takes a concrete `AgentApi` today; rebuilding the `AgentRepository` per call would change the data layer. **Resolution:** the construction in `init()` builds the `AgentApi` with the **initial** (url, token). A re-provision requires re-creating the `RepositoryModule` singleton (which already happens on app restart). The same simplification the Commands plan accepted for `CommandRepository` is reused here.

The exact code:

```kotlin
val agentRepository = AgentRepository(
    api = run {
        val c = clientProvider()
        AgentApi(baseUrl = c.baseUrl, token = c.token, client = c.client)
    },
)
```

This reads the (url, token) once at app startup. A re-provision requires a process restart for chat to pick up the new server. (Future slice: a `suspend () -> AgentApi` factory, same as the Commands plan uses for `CommandRepository`.)

### 8.3 — `Repositories` data class

Add two fields:

```kotlin
data class Repositories(
    val configuration: ConfigurationRepository,
    val session: SessionRepository,
    val device: DeviceRepository,
    val status: StatusRepository,
    val dashboard: DashboardRepository,
    val relayController: RelayController,
    val commandRepository: CommandRepository,
    // NEW:
    val agentRepository: AgentRepository,
    val chatHistoryStore: ChatHistoryStore,
)
```

### 8.4 — `repos` assignment

Add to the `repos = Repositories(...)` call at the end of `init()`:

```kotlin
agentRepository = agentRepository,
chatHistoryStore = chatHistoryStore,
```

---

## 9. Navigation

### 9.1 — `AppNavigation.kt`

Three new `composable(...)` entries. Imports:

```kotlin
import androidx.navigation.NavType
import androidx.navigation.navArgument
import com.sense.relay.ui.chat.ChatRoute
import com.sense.relay.ui.memory.MemoryRoute
import com.sense.relay.ui.atom.AtomDetailRoute
```

Entries inside the `NavHost { ... }` block (in this order):

```kotlin
// Chat — 6th bottom-bar tab. Chip taps deep-link to AtomDetail;
// refuse-link deep-links to Memory.
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

// Memory — stub for this slice. Reachable from Chat's refuse-link
// and (future) directly. The full MemoryScreen is its own slice.
composable(Destination.Memory.route) { MemoryRoute() }

// AtomDetail — stub for this slice. Reachable from Chat's chip-tap.
// The full AtomDetailScreen is its own slice.
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
        AtomDetailRoute(atomId = atomId, onBack = { navController.popBackStack() })
    }
}
```

`MAIN_ROUTES` gains `Destination.Chat.route`:

```kotlin
private val MAIN_ROUTES = setOf(
    Destination.Home.route,
    Destination.Recordings.route,
    Destination.Device.route,
    Destination.Commands.route,
    Destination.Chat.route,        // NEW (6th tab — INV-13 deviation)
    Destination.Settings.route,
)
```

### 9.2 — `BottomBar.kt`

Add an import:

```kotlin
import androidx.compose.material.icons.automirrored.filled.Chat
```

Add a `MainTab` entry in `mainDestinations` (between `Commands` and `Settings`, per the user's 6th-tab decision):

```kotlin
private val mainDestinations: List<MainTab> = listOf(
    MainTab(Destination.Home, "Home", Icons.Filled.Home),
    MainTab(Destination.Recordings, "Recordings", Icons.AutoMirrored.Filled.List),
    MainTab(Destination.Device, "Device", Icons.Filled.Info),
    MainTab(Destination.Commands, "Commands", Icons.Filled.PlayArrow),
    MainTab(Destination.Chat, "Chat", Icons.AutoMirrored.Filled.Chat),  // NEW
    MainTab(Destination.Settings, "Settings", Icons.Filled.Settings),
)
```

**Inline comment documenting the INV-13 deviation** (so a future reader sees the rationale without a git archeology expedition):

```kotlin
// INV-13 deviation: 6 tabs (spec says 4). Chat was added in 2026-07
// as a P2-answers user-facing surface. The future consolidation
// (drop Device or Commands from the bar; deep-link from Home) is
// tracked as a follow-up. See
// docs/superpowers/specs/2026-07-17-sense-android-chat-screen-design.md
// §5.1.
```

### 9.3 — `Destination.kt` — `ARG_ATOM_ID` constant

```kotlin
data class AtomDetail(val atomId: String) : Destination {
    override val route: String = "memory/atom/$atomId"
    companion object {
        const val ARG_ATOM_ID = "atomId"     // NEW — nav-arg key, named in one place
        fun build(atomId: String) = AtomDetail(atomId)
    }
}
```

---

## 10. `MemoryRoute` and `AtomDetailRoute` — the stubs

Both are deliberately minimal. The real screens are follow-up slices.

### 10.1 — `MemoryRoute` / `MemoryScreen`

```kotlin
// ui/memory/MemoryScreen.kt
@Composable
fun MemoryScreen(modifier: Modifier = Modifier) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Memory"))
        EmptyState(
            title = "Memory",
            body = "Browse and search your memories — coming soon.",
            modifier = Modifier.fillMaxSize().testTag("memory_empty"),
        )
    }
}

// ui/memory/MemoryRoute.kt
@Composable
fun MemoryRoute(modifier: Modifier = Modifier) {
    MemoryScreen(modifier = modifier)
}
```

### 10.2 — `AtomDetailRoute` / `AtomDetailScreen`

The atom detail stub looks up the chip text from the most-recent agent message in `ChatHistoryStore` (cheap heuristic — the real detail screen will fetch `/memory/{atomId}`). If the atom isn't found, it shows "Atom not found" via the same `EmptyState` primitive.

```kotlin
// ui/atom/AtomDetailScreen.kt
@Composable
fun AtomDetailScreen(
    atomId: String,
    text: String?,
    modifier: Modifier = Modifier,
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Atom", onBack = null))
        if (text == null) {
            EmptyState(
                title = "Atom $atomId",
                body = "This atom isn't in the current session history.",
                modifier = Modifier.fillMaxSize().testTag("atom_not_found"),
            )
        } else {
            Column(modifier = Modifier.padding(Spacing.md).testTag("atom_detail")) {
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

// ui/atom/AtomDetailRoute.kt
@Composable
fun AtomDetailRoute(
    atomId: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    // Cheap heuristic: find the atom's text in the most-recent agent
    // message that has atoms. The real detail screen will fetch
    // /memory/{atomId} from the server.
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
}
```

---

## 11. Error handling

| Failure | Surface |
|---------|---------|
| `POST /agent` returns network error | `AgentRepository.ask()` returns `AgentOutcome.Error(code = UNREACHABLE)` → mapped to `ChatMessage(kind = AGENT_ERROR, text = "Error: Can't reach the server")` → `ErrorMessageBubble` renders "Can't reach the server" (via `ErrorMapper` in the route; here the message comes pre-mapped from the VM's `outcome.message`). |
| `POST /agent` returns 5xx | Same path; `ApiError.Http(5xx)` → `ErrorMapper` → "Server error (5xx)" or "Server is starting up" (503). |
| `POST /agent` returns 401 | `ApiError.Unauthorized` → "Token rejected — sign in again". User must re-provision via Settings. |
| `POST /agent` returns 429 (rate limit) | `ApiError.Http(429)` → "Couldn't reach the server (429)" (per existing `ErrorMapper`). Future polish: a rate-limit-specific copy. |
| `POST /agent` returns 200 with malformed body | `AgentApi` throws `HttpApiError(INTERNAL_ERROR, "malformed response: ...")` → `ErrorMessageBubble`. |
| App rotated while waiting for agent | ViewModel survives; `loading` stays true; thinking bubble stays; draft is in `SavedStateHandle`. |
| App backgrounded while waiting | `viewModelScope` keeps the network call alive; the user comes back to a fresh agent message. |
| Process death while waiting | The user message is in `ChatHistoryStore` (in-memory) but lost on process death. The DataStore-backed version of `ChatHistoryStore` (future slice) will preserve it. |
| User taps a chip → `AtomDetailRoute` shows "atom not in history" | The chip is from a previous session that was cleared; the stub shows "This atom isn't in the current session history." Real detail screen (future slice) will fetch from the server. |
| User taps "Browse memory" on a refuse → `MemoryRoute` shows the "coming soon" empty state | Acceptable for this slice; the real MemoryScreen is a follow-up. |

---

## 12. Edge cases

1. **Empty history** — `EmptyState("Ask the agent", "Try …")` + the input bar.
2. **Long agent answer** — `LazyColumn` scrolls; auto-scroll to bottom on append.
3. **Many messages** — `LazyColumn` is virtualized; only the visible bubbles compose. No upper bound tested (chat history is in-memory, expected upper bound is low).
4. **Tap send with blank text** — `canSend = false`; button is disabled; no `onSend` fires.
5. **Tap send while loading** — `canSend = false`; no second `ask()` fires. (Multi-message in flight is not supported.)
6. **Draft contains only whitespace** — `canSend = draft.trim().isNotEmpty() && !loading` — whitespace-only is treated as blank.
7. **User rotates device while typing** — `SavedStateHandle` preserves the draft; the message list is in the `ChatHistoryStore` (in-memory, ViewModel-lifetime).
8. **User rotates device while a `ThinkingBubble` is animating** — Compose recomposes; the `rememberInfiniteTransition` restarts from `initialValue = 0.3f` (the visual blip is acceptable; this is not a regression from the current behavior).
9. **Process death** — history is lost; future DataStore version preserves it.
10. **Re-provision mid-session** — Chat is built with the startup (url, token); a re-provision takes effect on the next app launch. Same simplification as the Commands plan.
11. **Atom chip text longer than 80 chars** — truncated at the call site (`AgentMessageBubble`); full text is on the server, accessible via the future AtomDetail screen.
12. **Atom chip text empty / blank** — `AtomChip` renders a small empty pill; harmless. A future spec may filter empty chips.

---

## 13. Testing

### 13.1 — Unit tests (host-JVM, fast — no Robolectric)

**`ChatMessageKindTest.kt`** — round-trip the enum (4 cases, one per value). Exhaustiveness is verified by the `when` in `ChatMessageList` compiling; the test asserts the names + ordinals are stable.

**`ChatViewModelTest.kt` — 4 new one-line `kind` assertions** added to the existing 4 outcome tests:

```kotlin
// In the existing "ask sends user message and receives answer" test:
assertEquals(ChatMessageKind.USER_TEXT, msgs[0].kind)
assertEquals(ChatMessageKind.AGENT_ANSWER, msgs[1].kind)

// In the "ask with refuse outcome renders refusal message" test:
assertEquals(ChatMessageKind.AGENT_REFUSE, msgs[1].kind)

// In the "ask with error outcome shows error message" test:
assertEquals(ChatMessageKind.AGENT_ERROR, msgs[1].kind)
```

The existing 7 tests continue to pass; no new behavior is tested in the VM (the kind assignment is a one-line tag).

### 13.2 — Compose UI tests (`createComposeRule`)

**`ChatInputBarTest.kt`** (4 cases):
- Tapping the input field changes the text (via `onTextChanged`).
- Tapping the send button fires `onSend`.
- Send button is disabled when `canSend = false` (blank draft).
- Send button shows a spinner when `isLoading = true` (and is non-interactive).

**`UserMessageBubbleTest.kt`** (1 case):
- Renders the text exactly; `user_bubble` testTag is present.

**`AgentMessageBubbleTest.kt`** (3 cases):
- Renders the text exactly.
- Empty atoms list renders no `atom_chip` testTag.
- Non-empty atoms list renders one `atom_chip` per atom; tapping a chip fires `onAtomChipTap(atomId)`.

**`ThinkingBubbleTest.kt`** (1 case):
- Renders three dots with the `thinking_dot_0/1/2` testTags; does not crash on recompose (advances a state flag, recomposes, asserts the dots are still there).

**`RefuseMessageBubbleTest.kt`** (2 cases):
- The friendly copy is rendered ("I don't have a memory about that yet").
- Tapping the "browse memory directly" button fires `onBrowseMemory`.

**`ErrorMessageBubbleTest.kt`** (1 case):
- Renders the passed-in `message` verbatim (proves no double-mapping).

**`AtomChipTest.kt`** (2 cases):
- Text renders; tap fires `onClick`.
- (Truncation is a call-site concern in `AgentMessageBubble`, not in the chip — covered indirectly by `AgentMessageBubbleTest`.)

**`ChatMessageListTest.kt`** (4 cases):
- Auto-scrolls to bottom on `messages.size` increase.
- Per-variant dispatch: USER → `UserMessageBubble`, AGENT_ANSWER → `AgentMessageBubble`, AGENT_REFUSE → `RefuseMessageBubble`, AGENT_ERROR → `ErrorMessageBubble`. (Asserted by checking each bubble's unique testTag is present in the tree.)
- `isThinking=true` appends a `ThinkingBubble` after the last message; `isThinking=false` does not.
- `key = it.id` is stable: a recompose with the same messages doesn't throw or duplicate bubbles.

**`ChatScreenTest.kt`** (7 cases):
- Empty state: `messages = emptyList()` → `chat_empty` testTag present, no `chat_list`.
- User-only: 1 user message → `user_bubble` rendered, input bar present.
- User + agent: 2 messages → both bubbles rendered.
- Loading + last is USER → `thinking_bubble` rendered in addition to the user bubble.
- Loading + last is AGENT → no thinking bubble (already replied).
- Refuse: last message is `AGENT_REFUSE` → `refuse_bubble` rendered, `browse memory` link present.
- Error: last message is `AGENT_ERROR` → `error_bubble` rendered with the message.
- Chip tap forwards to `onAtomChipTap`.

### 13.3 — Architectural invariant

The existing `ArchitecturalInvariantsTest` (file-tree scan of `ui/` for `import com.sense.relay.http.*`) covers INV-11 for the chat surface. No new test is required; if a future change adds an http import to a chat file, the existing test fails.

### 13.4 — What this slice does NOT add

- No instrumentation tests.
- No Paparazzi / screenshot tests.
- No new fakes for `ChatHistoryStore` or `AgentRepository` (the existing `FakeAgentRepo` in `ChatViewModelTest.kt` is sufficient).
- No visual regression baseline.
- No end-to-end test (the server-side E2E gate `test_cognitive_read_path_end_to_end` is unchanged and still green).

---

## 14. Design principles (binding for the plan)

- **The Composable is a view.** No business state, no filter state, no derived state. It collects, branches, dispatches.
- **The ViewModel owns the lifecycle of the data layer.** The existing `ChatViewModel.ask()` is the only mutation surface; this slice adds zero new VM methods.
- **The data layer is the contract.** `AgentRepository.ask()` and `ChatHistoryStore.messages` are the only seams. The screen never imports `HttpApiError` (INV-11), `ErrorCode`, or any DTO.
- **Monochrome UI.** Bubbles use `primaryContainer` (user) and `surfaceVariant` (agent / thinking / refuse / error). No per-status colors. The status text is the signal.
- **Exhaustive dispatch.** The bubble dispatch is `when (msg.kind)` — a future variant is a compile error in `ChatMessageList.kt`, which is the right failure mode.
- **Optimistic UI.** User message appears immediately; thinking bubble appears during the request; agent reply replaces it on response.
- **No polling.** Chat is user-initiated; the lifecycle effect is just collecting state, not polling.
- **Existing patterns.** Reuses `SenseTopBar`, `EmptyState`, `Spacing`, `TouchTarget`. The Route+Screen separation matches `HomeRoute`/`DeviceRoute`/`CommandsRoute`.
- **Architectural invariant preserved.** INV-11 holds; the existing `ArchitecturalInvariantsTest` enforces it.
- **TDD throughout.** Each task that adds behavior writes a failing test first.
- **No rewrites of working code.** Extend, append. The data layer is the contract; the screen is the surface.

---

## 15. Phasing (single chunk; this spec is one PR)

This is **one** Android-only chunk, intentionally not broken into sub-phases. The data layer, the ViewModel, the existing tests, and the existing nav infrastructure are all in place. The chunk is:

- 1 data-layer edit (`kind` field on `ChatMessage`)
- 1 VM edit (2 small changes: `messages` passthrough + 3 `kind` assignments)
- 1 DI wire (`agentRepository` + `chatHistoryStore` in `RepositoryModule.repos`)
- 9 new Composables + 1 Route + 1 Screen wrapper
- 2 stub Routes (`MemoryRoute` + `AtomDetailRoute`)
- 3 nav-graph edits (`AppNavigation` + `BottomBar` + `Destination.ARG_ATOM_ID`)
- 10 new test files + 4 new VM-test assertions
- 1 architectural-invariant test (already in place, no change)

Estimated: 11 commits on `main`, each green. Working style: contracts first (the Composable signatures are the contract), then TDD on each bubble, then the Screen, then the Route, then the nav wiring.

**Working style:** the Composable signatures (Section 3 + 4 + 5 + 6) are the binding contract. The plan will write tests against those signatures, then implementations, then wire the Route, then wire the nav graph. `main` stays green at every commit.

---

## 16. Non-goals / deferred

| Deferred | Why |
|----------|-----|
| Real `MemoryScreen` (browse / search atoms) | The refuse-link lands the route as a stub; the real screen is its own design → plan → implement cycle (item #2 in `project-status.md`). |
| Real `AtomDetailScreen` (full atom text + provenance + jump-to-session) | The chip-tap lands the route as a stub. The heuristic lookup in `ChatHistoryStore` is the stub's fallback; the real screen will fetch `/memory/{atomId}`. |
| DataStore-backed `ChatHistoryStore` | The in-memory version is the contract for this slice; the `flush()` seam is in place for the future version. Process death loses history. |
| Streaming LLM responses | `AgentRepository.ask()` is a single request/response today. A future streaming slice adds `Flow<String>` to the repo. |
| Multi-session chat | `ChatViewModel(sessionId)` exists; this slice always passes `null`. Multi-session is a future slice. |
| Markdown rendering of agent answers | Plain text only. A future slice adds a markdown Composable; `AgentMessageBubble` would accept an optional `renderer` parameter. |
| Voice input | Out of scope (the wearable captures audio; the relay receives it). |
| Per-message copy / share / delete | Out of scope. History is append-only. |
| Message editing / re-ask | Out of scope. Architectural invariant: history is append-only. |
| Rate-limit-specific copy | The generic `ErrorMapper` message is used; rate-limit-specific copy is a future polish. |
| Animations on new-message append | The list auto-scrolls; no entry/exit animations. The project uses subtle defaults. |
| Bottom-bar consolidation to 4 tabs (INV-13) | Tracked as a deviation; the user chose 6 tabs in brainstorming. A future slice (P3+) revisits. |
| `SetupActivity` INV-11 tech debt | Tracked as item #6 in `project-status.md`; not in this slice. |
| Localization | English-only, matching the rest of the app. |

---

## 17. Spec self-review (placeholders, internal consistency, scope, ambiguity)

**1. Placeholder scan.** No "TBD", no "TODO", no "implement later". The "coming soon" copy in `MemoryScreen` and `AtomDetailScreen` is intentional user-facing copy, not a placeholder for a missing feature; both screens are reachable and render real content.

**2. Internal consistency.**
- Section 1 says the only data-layer change is the `kind` field. Section 7 confirms the change. Section 13 confirms the test coverage.
- Section 3 (`ChatScreen` signature) lists `onAtomChipTap` and `onBrowseMemory` as callbacks. Section 9 (`AppNavigation`) wires them. Section 6 (`AgentMessageBubble` + `RefuseMessageBubble`) consumes them. Consistent.
- Section 4 (`ChatMessageList`) uses `msg.kind`; Section 7 defines `ChatMessageKind`. Consistent.
- Section 8 builds `AgentRepository` with the initial (url, token); Section 10 (`AtomDetailRoute`) reads from `RepositoryModule.repos.chatHistoryStore`. Both are wired into `RepositoryModule` in Section 8. Consistent.

**3. Scope check.** Single feature, ~11 commits, ~17 new files + 6 edited files, ~26 new tests. Matches the Commands plan's shape (10 commits, ~25 tests, 14 files). Appropriately scoped for a single plan.

**4. Ambiguity check.**
- "Send button only" — confirmed in brainstorming (Section 3 of brainstorm). No Enter-to-send.
- "Animated 3-dot pulse" — confirmed; `rememberInfiniteTransition` is the mechanism.
- "Friendly copy + Browse memory link" — confirmed; the copy is quoted in Section 6.4.
- "Inline chips, tap → stub detail" — confirmed; the stub is in Section 10.2.
- "ViewModel-lifetime only" — confirmed; `ChatHistoryStore` is process-singleton but in-memory.
- "6th tab in the bottom bar" — confirmed; the INV-13 deviation is documented in `BottomBar.kt`.
- "Build AgentApi with initial (url, token) at app startup" — explicit in Section 8.2; a re-provision requires a process restart for chat to pick up the new server. Same simplification as the Commands plan.

No outstanding issues. Spec is ready for user review.
