# Sense — P3 Proactive Trigger

> **Goal:** add a `Proactive` trigger source to the planner so the server can push an unsolicited agent answer to the phone when extraction completes on a session, without the user asking a question. Best-effort delivery over the existing gateway WebSocket. No new transport, no new dependencies, no new auth.

## 1. Context

The Sense server's P2-answers (chat) and P2-commands (device commands) flows are end-to-end working: the phone sends a `POST /agent`, the planner produces a `PlannerResult`, the phone renders it. The planner is currently driven by exactly one trigger source — a user question from the phone, via `POST /agent` (the only producer of `PlannerContext`).

The 2026-07-07 design spec (`docs/superpowers/specs/2026-07-07-sense-agent-memory-design.md:18, 42`) defines a second trigger source — `Proactive(session_id, event_id, transcript)` — that fires when the extraction worker finishes indexing new atoms for a session. The intent is that the server can surface something the user said or heard *without* the user asking, e.g. "you mentioned X earlier; here it is now" or "reminder of the goal you set." That was always a goal of the memory platform and was explicitly out of scope for the 2026-07-19 wiring fix.

This slice delivers P3. The constraint that shapes the design: **the server has no outbound channel to the phone today.** The only transport the server can use to reach the phone is the already-open gateway WebSocket, which exists only while the relay is connected. P3 accepts that constraint and ships best-effort: if the relay is disconnected, the proactive is dropped with a metric. A real push channel (FCM, APNs, SSE, long-poll) is a separate future slice.

## 2. Architecture

Five new components, all internal to the existing subsystems, no new external services:

1. **Trigger envelope** on the planner's public seam. `PlannerContext.trigger_text: str` is replaced with `trigger: Trigger`, where `Trigger = UserRequest(request_id, text) | Proactive(request_id, event_id, transcript)`. The `PlannerLike` Protocol's `plan(ctx: PlannerContext) -> PlannerResult` signature is unchanged in shape — the unification rule holds. `ContextBuilder.build(trigger, retrieved, capabilities)` accepts the envelope.

2. **Planner restriction.** Under a `Proactive` trigger, the planner is permitted `RETURN` and `REFUSE` outcomes only. `ISSUE_COMMAND` is *forbidden* — a proactive call cannot cause a device action. Enforced inside `Planner.plan` by branching on `isinstance(ctx.trigger, Proactive)` and refusing to call `CommandDispatcher.issue`. Audit log records the outcome with `trigger_source: "proactive"`. This is a type-system guarantee, not a guardrail tweak.

3. **Listener list on the extraction worker.** `ExtractionWorker` gains a `listeners: list[Callable[[SessionCompletion], Awaitable[None]]]` slot. After the cursor advances successfully in `process_session`, the worker calls every listener with a `SessionCompletion(session_id, completed_at, event_id_range)`. The v1 policy is "fire on every successful extraction." The list-of-listeners design keeps the worker decoupled from any specific subscriber.

4. **ProactiveTriggerEngine** at `agent/proactive.py`. On a `SessionCompletion`, the engine builds a `PlannerContext(trigger=Proactive(...), session_id=..., limit=10)`, calls `planner.plan(ctx)`, and on `RETURN` calls `ws_sender.send_proactive(session_id, message)`; on `REFUSE` (or any other outcome, defensively) it logs and drops. The plan call runs in a bounded task with a 2-second timeout — proactive calls are best-effort and must not block extraction.

5. **Gateway WS extension.** A new `_pending_proactives()` method on `GatewayCore` (sibling of `_pending_commands()` at `gateway/core.py:116`). It pulls from a process-singleton `ProactiveOutbox` (a `asyncio.Queue` keyed by session) and yields `ProactiveMessage(session_id, request_id, text, atoms)` frames over the existing WS. The `MessageType` enum gains `PROACTIVE = "proactive"`. The frame is signed with the same gateway key the phone already trusts. **Best-effort:** if the relay is disconnected, the message sits in the outbox up to N seconds (default 30s) and is then dropped with `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=relay_disconnected}` incremented. If the relay reconnects, the outbox is drained on the first `_pending_proactives` pull.

**Phone surface.** A new `ChatMessageKind.AGENT_PROACTIVE` (sibling of `AGENT_ANSWER` / `AGENT_REFUSE` / `AGENT_ERROR`) on `ChatMessage`. The relay receives `ProactiveMessage` frames over the WS, deserializes them, and dispatches through the existing `ChatHistoryStore` flow. A new `ProactiveMessageBubble` Composable (sibling of `AgentMessageBubble` / `RefuseMessageBubble`) renders the message with the same bubble styling plus a small "Proactive" tag so the user knows it wasn't from a question they asked. Tappable atoms still work; dismissable; rendered inline in `ChatMessageList`. If the app is in the background, the message is queued in the existing chat history and surfaced when the user opens Chat — no system-level Android notification in v1 (consistent with the rest of the app, which is fully in-app).

## 3. Component details

### 3.1 `Trigger` envelope (frozen pydantic, in `contracts/types.py`)

```python
class UserRequest(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    text: str

class Proactive(BaseModel):
    model_config = ConfigDict(frozen=True)
    request_id: str
    event_id: str  # the transcript event that triggered this proactive
    transcript: str  # the raw transcript that produced the atoms

Trigger = Annotated[Union[UserRequest, Proactive], Field(discriminator="kind")]
```

`PlannerContext.trigger_text: str` is replaced with `trigger: Trigger`. The `request_id` is a fresh `IdGenerator.next()` call from the producer — for `UserRequest` it is the inbound request's id (preserved); for `Proactive` it is a new id minted by `ProactiveTriggerEngine`.

### 3.2 `Planner.plan` enforcement

`Planner.plan` keeps its single public signature. Inside, after the LLM returns its decision, the planner branches on the outcome:

- `RETURN` / `RETURN_WITH_UNCERTAINTY`: build a `PlannerResult` and return. (Same as today.)
- `REFUSE`: build a `PlannerResult` and return. (Same as today.)
- `ISSUE_COMMAND`: if `isinstance(ctx.trigger, Proactive)`, return `PlannerResult(outcome=REFUSE, ...)` with `reason="proactive_trigger_cannot_issue_command"`. Otherwise dispatch as today. The audit log records `trigger_source="proactive"` and `outcome="refuse"` and `reason="proactive_trigger_cannot_issue_command"`.

The branch happens *before* `CommandValidator.validate`, *before* `CommandGuardrails.evaluate`, *before* `CommandDispatcher.issue` — the proactive restriction is the first thing checked.

### 3.3 `SessionCompletion` + worker listener list (in `memory/extraction_worker.py`)

```python
@dataclass(frozen=True)
class SessionCompletion:
    session_id: str
    completed_at: datetime  # injected via clock
    event_id_range: tuple[int, int]  # inclusive (first, last) event seq in the batch

class ExtractionWorker:
    def __init__(self, ..., listeners: list[Callable[[SessionCompletion], Awaitable[None]]] | None = None):
        self._listeners = list(listeners or [])

    def add_listener(self, listener: Callable[[SessionCompletion], Awaitable[None]]) -> None: ...
    def remove_listener(self, listener: Callable[[SessionCompletion], Awaitable[None]]) -> None: ...
```

In `process_session`, after the cursor advances:

```python
if cursor_advanced:
    completion = SessionCompletion(
        session_id=session_id,
        completed_at=self._clock.now(),
        event_id_range=(first_seq, last_seq),
    )
    for listener in list(self._listeners):  # snapshot to allow removal during dispatch
        try:
            await listener(completion)
        except Exception:
            # H7 invariant: a listener failure must not block the worker.
            # Log + metric, do not re-raise.
            log.exception("extraction_listener_failed", extra={"session_id": session_id})
            metrics.inc("EXTRACTION_LISTENER_FAILURE_TOTAL")
```

The snapshot (`list(self._listeners)`) is required so a listener that calls `remove_listener` during dispatch doesn't mutate the iteration. The try/except is the worker saying "I don't care about your result; my cursor is advanced, I'm done."

### 3.4 `ProactiveTriggerEngine` (new module, `agent/proactive.py`)

```python
class ProactiveTriggerEngine:
    def __init__(
        self,
        planner: Planner,
        ws_sender: WsSender,  # new protocol, the gateway implements
        clock: Clock,
        metrics: Metrics,
        ids: IdGenerator,
        plan_timeout_s: float = 2.0,
    ): ...

    async def on_session_completion(self, completion: SessionCompletion) -> None:
        try:
            async with asyncio.timeout(self._plan_timeout_s):
                result = await self._planner.plan(PlannerContext(
                    request_id=self._ids.next(),
                    trigger=Proactive(
                        request_id=self._ids.next(),
                        event_id=f"event_seq_{completion.event_id_range[1]}",
                        transcript="",  # v1: transcript replay is a follow-up; the planner uses session memory
                    ),
                    session_id=completion.session_id,
                    limit=10,
                ))
        except (asyncio.TimeoutError, Exception):
            log.exception("proactive_plan_failed", extra={"session_id": completion.session_id})
            self._metrics.inc("PROACTIVE_PLAN_FAILURE_TOTAL")
            return

        if result.outcome in (PlannerOutcome.RETURN, PlannerOutcome.RETURN_WITH_UNCERTAINTY):
            try:
                await self._ws_sender.send_proactive(
                    session_id=completion.session_id,
                    request_id=result.request_id,
                    text=result.answer,
                    atoms=result.atoms,
                )
                self._metrics.inc("PROACTIVE_DELIVERED_TOTAL")
            except Exception:
                log.exception("proactive_send_failed", extra={"session_id": completion.session_id})
                self._metrics.inc("PROACTIVE_SEND_FAILURE_TOTAL")
            return

        # REFUSE or anything else: drop with a counter.
        self._metrics.inc("PROACTIVE_REFUSED_TOTAL")
```

The engine is wired in `scripts/run_gateway.py` as a listener on the existing extraction worker instance, immediately after the worker is constructed. The `WsSender` is a new protocol that the `GatewayCore` implements.

### 3.5 `ProactiveOutbox` + WS delivery (in `gateway/core.py` + `gateway/adapter.py`)

```python
class ProactiveOutbox:
    def __init__(self, ttl_s: float = 30.0, clock: Clock):
        self._ttl_s = ttl_s
        self._clock = clock
        self._by_session: dict[str, list[tuple[float, ProactiveMessage]]] = {}  # (expires_at, msg)

    def enqueue(self, msg: ProactiveMessage) -> None: ...
    def drain(self, session_id: str) -> list[ProactiveMessage]: ...
    def evict_expired(self) -> int: ...  # returns count evicted, for the dropped metric
```

`GatewayCore` gains a `_proactive_outbox: ProactiveOutbox` and a `_pending_proactives()` coroutine that runs alongside `_pending_commands()` in `adapter.py`'s `serve(...)` loop:

```python
async def _pending_proactives(self):
    while True:
        await self._proactive_event.wait()  # set by enqueue, cleared when drained
        msgs = self._proactive_outbox.drain(self._session_id)
        for m in msgs:
            yield m
```

The outbox `ttl_s=30.0` is enforced in `enqueue` — if a message would expire before delivery can be attempted, it is dropped with `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}` and never enters the queue. The outbox is also evicted lazily on each `drain` call (any entries past their `expires_at` are removed and counted).

**Why an in-process outbox, not a table?** A SQLite-backed outbox would survive a server restart, but v1 is best-effort over a single-process gateway. A restart drops everything in flight, which is acceptable given the metric. Persistence is a follow-up.

### 3.6 Phone side (Android, `app/src/main/kotlin/com/sense/relay/...`)

A new `ChatMessageKind.AGENT_PROACTIVE` (frozen enum). A new `ProactiveMessageBubble` Composable:

```kotlin
@Composable
private fun ProactiveMessageBubble(message: ChatMessage) {
    Column(modifier = Modifier
        .fillMaxWidth()
        .padding(Spacing.sm)
        .testTag("chat_proactive_${message.id}")) {
        Row(verticalAlignment = Alignment.CenterVertically) {
            Icon(Icons.Filled.Notifications, contentDescription = "Proactive")
            Spacer(Modifier.width(Spacing.xs))
            Text("Proactive", style = MaterialTheme.typography.labelSmall)
        }
        Spacer(Modifier.height(Spacing.xs))
        AgentMessageBody(message)  // shared with AgentMessageBubble
    }
}
```

The relay's existing `ChatHistoryStore` already supports any `ChatMessageKind`; the new kind flows through the same `when (msg.kind)` dispatch in `ChatMessageList`. Tappable atoms reuse the existing `AtomChip` Composable. No new navigation, no new top-level route.

## 4. File map

**Server — new files:**
- `src/sense_server/agent/proactive.py` — `ProactiveTriggerEngine`, `WsSender` Protocol
- `src/sense_server/contracts/proactive_dto.py` — `ProactiveMessage` wire DTO (lives alongside the existing `commands_dto.py`)
- `tests/agent/test_proactive_engine.py`
- `tests/agent/test_planner_proactive.py`
- `tests/memory/test_extraction_worker_listeners.py`
- `tests/gateway/test_proactive_outbox.py`
- `tests/gateway/test_proactive_ws.py`

**Server — modified files:**
- `src/sense_server/contracts/types.py` — add `UserRequest`, `Proactive`, `Trigger`; replace `PlannerContext.trigger_text` with `trigger: Trigger`
- `src/sense_server/agent/planner.py` — proactive restriction in `plan()`
- `src/sense_server/agent/context.py` — `ContextBuilder.build` accepts `Trigger` envelope
- `src/sense_server/agent/audit.py` — record `trigger_source` in audit log
- `src/sense_server/agent/metrics.py` — add `PROACTIVE_DELIVERED_TOTAL`, `PROACTIVE_REFUSED_TOTAL`, `PROACTIVE_PLAN_FAILURE_TOTAL`, `PROACTIVE_SEND_FAILURE_TOTAL`, `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason}`, `EXTRACTION_LISTENER_FAILURE_TOTAL`
- `src/sense_server/memory/extraction_worker.py` — `SessionCompletion`, `add_listener` / `remove_listener`, listener dispatch in `process_session`
- `src/sense_server/gateway/core.py` — `ProactiveOutbox`, `_pending_proactives`, `WsSender` implementation
- `src/sense_server/gateway/adapter.py` — register `_pending_proactives` in the `serve()` loop
- `src/sense_server/contracts/messages.py` — add `MessageType.PROACTIVE = "proactive"` and the `ProactiveMessage` envelope
- `scripts/run_gateway.py` — wire `ProactiveTriggerEngine` as a listener on the extraction worker; pass `plan_timeout_s`
- `tests/agent/test_planner.py` — update existing tests to construct `Trigger` envelopes (the public signature is changing)
- `tests/agent/test_planner_issue_command.py` — same
- `tests/http/test_agent_route.py` — update route test for new `Trigger` shape

**Android — new files:**
- `app/src/main/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubble.kt`
- `app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt`

**Android — modified files:**
- `app/src/main/kotlin/com/sense/relay/data/ChatMessage.kt` — add `AGENT_PROACTIVE` to `ChatMessageKind`
- `app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt` — add the new branch to the `when (msg.kind)` dispatch
- `app/src/main/kotlin/com/sense/relay/ws/MessageType.kt` (or wherever the relay-side frame types live) — add `Proactive` parsing

## 5. Test strategy

Five new server test files and one new Android test file. All follow the existing project style (fakes, asyncio, frozen pydantic).

- **`tests/agent/test_proactive_engine.py`** — engine builds the correct `PlannerContext` (right `request_id` from `IdGenerator`, right `event_id` from `event_id_range`, right `session_id`); drops on `REFUSE`; forwards `RETURN` to `ws_sender.send_proactive`; logs + counts on timeout; never invokes `ws_sender` when the planner would `ISSUE_COMMAND` (defensive test of the planner-side enforcement, asserted again here from the engine side).
- **`tests/agent/test_planner_proactive.py`** — `Planner.plan` under `Proactive` trigger: `RETURN` works as today; `REFUSE` works as today; `ISSUE_COMMAND` from the LLM is *refused by the planner* and never reaches `CommandDispatcher.issue`; audit log records `trigger_source="proactive"` for every proactive outcome.
- **`tests/memory/test_extraction_worker_listeners.py`** — listener list is invoked after cursor advance; not invoked when `process_session` raises (H7 invariant — cursor never advances on failure); exception in a listener does not block the worker or other listeners; `add_listener` / `remove_listener` work; iterating a snapshot means in-iteration removal is safe.
- **`tests/gateway/test_proactive_outbox.py`** — `enqueue` stores; `drain` returns; `drain` evicts expired entries and counts them in `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}`; `drain` returns empty for an unknown session.
- **`tests/gateway/test_proactive_ws.py`** — `_pending_proactives` yields queued messages; TTL expiry drops with the right metric reason; on WS reconnect, outbox is drained (no duplicates).
- **`app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt`** — bubble renders the proactive tag + the answer text + the atom chips; has the `chat_proactive_<id>` testTag (matches the convention for the other bubbles).

Plus updates to the existing planner + route tests to construct `Trigger` envelopes. The public signature is changing, so every existing call site must move from `trigger_text="hello"` to `trigger=UserRequest(request_id="r", text="hello")`.

## 6. Out of scope (explicit non-goals for v1)

- **No new transport.** FCM, APNs, SSE, long-poll. A real push channel is a separate future slice. v1 only reuses the already-open gateway WS.
- **No system-level Android notification.** Proactives are in-app only. Consistent with the rest of the app.
- **No heuristic / keyword gate.** Every extraction fires a plan call; the LLM is the gate. A cheap keyword filter (e.g. "remind", "tomorrow", "by Friday") would reduce planner load but adds code and tests; deferred.
- **No new atom kinds.** No `REMINDER` kind. The 2026-07-07 spec's proactive prompt is reused as-is.
- **No persistence past the in-process outbox.** A server restart drops everything in flight, which is acceptable given the metric. SQLite-backed outbox is a follow-up.
- **No transcript replay into the proactive trigger.** v1 sends an empty transcript string; the planner retrieves from session memory. Capturing the exact transcript that triggered the extraction is a follow-up.

## 7. Estimated size

~2-3 days, dominated by the planner restriction (with audit changes) and the WS outbox. The listener list on the worker is the smallest piece (~30 lines + tests). The Android side is a single bubble + one new enum value + one new dispatch branch, ~0.5 day including tests.

## 8. Risk + invariants

- **H7 (extraction cursor):** the listener list must only fire on successful extraction. The dispatch is *after* the cursor advance, inside the same try block; failures inside the dispatch are caught and counted, never re-raised. The test `test_extraction_worker_listeners.py` asserts this directly.
- **INV-11 (Android UI boundary):** the new `ProactiveMessageBubble` lives in `ui/chat/` and must not import `http.*`. Enforced by the existing `ArchitecturalInvariantsTest`.
- **No new dependencies.** No pip changes, no Gradle changes.
- **Type-system guarantee on command restriction.** A `Proactive` trigger cannot produce a `Command` because `Planner.plan` returns `REFUSE` before reaching `CommandDispatcher.issue`. The test `test_planner_proactive.py` asserts that `CommandDispatcher.issue` is never called on the proactive path with a fake dispatcher.
- **Best-effort is observable.** Every drop has a metric: `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason}` covers TTL expiry, relay disconnected past TTL, and planner refusal. The phone never sees a proactive that "vanished" without a counter incrementing.
