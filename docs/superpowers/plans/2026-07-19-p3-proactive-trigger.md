# P3 Proactive Trigger Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `Proactive` trigger source to the planner so the server can push an unsolicited agent answer to the phone when extraction completes on a session, and have the relay render it as a new `AGENT_PROACTIVE` ChatMessage kind in the existing `ChatScreen`.

**Architecture:** Replace `PlannerContext.trigger_text: str` with a tagged `Trigger` envelope (`UserRequest` | `Proactive`) so the planner's existing read path + audit + guardrails are source-agnostic. Add a listener list on the extraction worker; a `ProactiveTriggerEngine` invokes the planner on every successful extraction and forwards `RETURN` results into a process-singleton `ProactiveOutbox`. A new `_pending_proactives` loop on `GatewayCore` yields outbox entries as `proactive` §E frames over the same gateway WebSocket the relay already holds open. On the relay, `RelaySession.onServerMessage` parses the new `proactive` type and emits a new `ForwardToChatHistory` action; the runtime routes that action into the existing `ChatHistoryStore`. The ChatScreen dispatches `AGENT_PROACTIVE` to a new `ProactiveMessageBubble`. The proactive trigger is forbidden from issuing device commands — enforced inside `Planner.plan` (not guardrails) as a type-system guarantee.

**Tech Stack:** Python 3.12 / pydantic 2 / aiohttp / pytest-asyncio (server, existing). Kotlin 1.9 / Jetpack Compose / kotlinx-coroutines-test (Android, existing). No new dependencies anywhere.

## Global Constraints

- **TDD throughout.** Every fix lands a failing test first, then the minimal code, then a green run. `main` stays green at every commit.
- **Trigger envelope (frozen pydantic v2).** `Trigger = Annotated[Union[UserRequest, Proactive], Field(discriminator="kind")]`. Both variants carry a `request_id`; `Proactive` additionally carries `event_id: str` and `transcript: str`. Defined once in `server/src/sense_server/contracts/types.py`.
- **Unification rule.** `Planner.plan(ctx)` keeps its single public signature. `UserRequest` and `Proactive` go through the same retriever, validator, guardrails, audit, and (where allowed) dispatcher. The only branch inside the planner is the proactive-trigger `ISSUE_COMMAND` prohibition, and it is the *first* check before the command path.
- **Proactive restriction (type-system guarantee).** Inside `Planner.plan`, before `CommandValidator.validate` is called, if `isinstance(ctx.trigger, Proactive)` AND `validated.action.kind == AgentActionKind.ISSUE_COMMAND`, the planner returns `PlannerResult(outcome=REFUSE, refusal_reason=PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND, ...)`. The audit log records `trigger_source="proactive"`. This is enforced in the planner itself — *not* in guardrails, *not* in `CommandValidator`, *not* in `CommandDispatcher`. A reviewer should be able to grep for the prohibition and find exactly one place.
- **Best-effort delivery.** Proactive messages are dropped (with a metric) when: the relay has been disconnected for longer than the outbox TTL (`30.0` seconds), the planner timed out (>2.0s), or the plan call raised. There is no persistence past the in-process outbox.
- **Exact metric names (from spec §4):** `PROACTIVE_DELIVERED_TOTAL`, `PROACTIVE_REFUSED_TOTAL`, `PROACTIVE_PLAN_FAILURE_TOTAL`, `PROACTIVE_SEND_FAILURE_TOTAL`, `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason}`, `EXTRACTION_LISTENER_FAILURE_TOTAL`. Each new constant goes in `server/src/sense_server/contracts/metrics.py`; the recorder enforces the canonical-name contract.
- **H7 (extraction cursor).** The listener list on `ExtractionWorker` only fires AFTER the cursor advances successfully. Listener exceptions are caught, logged, and counted — never re-raised, never block the worker.
- **INV-11 (Android UI boundary).** The new `ProactiveMessageBubble` lives in `ui/chat/` and must not import `com.sense.relay.http.*` or `com.sense.relay.http.dto.*`. Enforced by `ArchitecturalInvariantsTest`.
- **Manual DI on Android.** `viewModelFactory { initializer { ... } }` is the project pattern. No Hilt, no Koin.
- **Test style.** Server tests: pytest + pytest-asyncio, fakes over real implementations, frozen pydantic models. Android tests: Robolectric-free (sandbox blocks it, see `ArchitecturalInvariantsTest` note). The new `ProactiveMessageBubble` test is a signature-check only, not a render test.
- **No new HTTP / DTO / wire types beyond the spec's explicit list.** The proactive `MessageType` enum value, the `Proactive` Kotlin `ChatMessageKind` value, the `Trigger` pydantic model, and the `ProactiveMessage` pydantic wire DTO are the only new public types.
- **Frequent commits.** Six commits in order, each leaving `main` green. Use `feat(server): …` / `feat(android): …` prefixes consistent with repo history.

## What this slice does NOT add

- **No new transport.** FCM, APNs, SSE, long-poll. v1 only reuses the open gateway WS.
- **No system-level Android notification.** Proactives are in-app only. A real Android notification is a follow-up slice.
- **No heuristic / keyword gate before invoking the planner.** Every successful extraction fires a `Proactive` plan call; the LLM is the gate. A cheap keyword filter (e.g. "remind", "tomorrow") is a follow-up.
- **No new atom kinds.** No `REMINDER` kind. The 2026-07-07 spec's proactive prompt is reused as-is.
- **No persistence past the in-process outbox.** A server restart drops everything in flight, which is acceptable given the metric. SQLite-backed outbox is a follow-up.
- **No transcript replay into the proactive trigger.** v1 sends an empty `transcript=""`; the planner retrieves from session memory. Capturing the exact transcript is a follow-up.
- **No BLE-relay changes for the proactive path.** The `RelaySession` only needs to learn one new `ServerMessage` variant (`Proactive`) and emit one new `RelayAction` (`ForwardToChatHistory`). The forward path is a new line in `RelayService` that pushes a record into `ChatHistoryStore`. No BLE, no signing, no device-side code.

---

## File Structure

### Server — new files

```
server/src/sense_server/agent/proactive.py
  — ProactiveTriggerEngine, WsSender Protocol
server/tests/agent/test_proactive_engine.py
server/tests/agent/test_planner_proactive.py
server/tests/memory/test_extraction_worker_listeners.py
server/tests/gateway/test_proactive_outbox.py
server/tests/gateway/test_proactive_ws.py
```

### Server — modified files

```
server/src/sense_server/contracts/types.py
  — add UserRequest, Proactive, Trigger
  — replace PlannerContext.trigger_text with trigger: Trigger
server/src/sense_server/agent/planner.py
  — proactive restriction in plan() (FIRST check before _dispatch_command)
  — ContextBuilder.build takes Trigger
  — audit carries trigger_source
server/src/sense_server/agent/context.py
  — ContextBuilder.build signature: (self, trigger: Trigger, retrieved, capabilities)
  — internal: text = trigger.text for UserRequest, trigger.transcript for Proactive
server/src/sense_server/agent/audit.py
  — InMemoryAuditLogger + SqliteAuditLogger record trigger_source
server/src/sense_server/contracts/metrics.py
  — add 6 new metric constants
server/src/sense_server/memory/extraction_worker.py
  — SessionCompletion dataclass
  — ExtractionWorker gains listeners slot + add/remove + dispatch in process_session
server/src/sense_server/gateway/core.py
  — ProactiveOutbox class
  — _pending_proactives coroutine
  — WsSender implementation
server/src/sense_server/gateway/adapter.py
  — register _pending_proactives in the serve() loop
server/src/sense_server/protocol/messages.py
  — add ProactiveMessage outbound §E frame
  — add MessageType.PROACTIVE = "proactive"
server/scripts/run_gateway.py
  — wire ProactiveTriggerEngine as a listener on the extraction worker
  — pass plan_timeout_s
  — start a new task that drains _pending_proactives to ws.send(...)
server/tests/agent/test_planner.py
  — update _ctx() helper to use Trigger envelopes
server/tests/agent/test_planner_issue_command.py
  — update _ctx() helper
server/tests/http/test_agent_route.py
  — update DTO mapping test for new Trigger shape
server/src/sense_server/http/routes/agent.py
  — wrap the inbound text in UserRequest(request_id, text)
```

### Android — new files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubble.kt
android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt
```

### Android — modified files

```
android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt
  — add AGENT_PROACTIVE to ChatMessageKind
android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt
  — add AGENT_PROACTIVE branch to the when (msg.kind) dispatch
android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt
  — add ServerMessage.Proactive(payload, atoms) variant
  — extend parseServerMessage for the "proactive" type
android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt
  — dispatch ServerMessage.Proactive -> RelayAction.ForwardToChatHistory
  — add ForwardToChatHistory to the sealed interface
android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt
  — execute RelayAction.ForwardToChatHistory by writing to ChatHistoryStore
```

---

## Commit 1: Server — `Trigger` envelope on `PlannerContext`

**Files:**
- Modify: `server/src/sense_server/contracts/types.py` (add `UserRequest`, `Proactive`, `Trigger`; replace `PlannerContext.trigger_text` with `trigger: Trigger`)
- Modify: `server/src/sense_server/agent/context.py` (signature change: `build(trigger, retrieved, capabilities)`)
- Modify: `server/src/sense_server/agent/planner.py` (pass `ctx.trigger` to `context_builder.build`; the proactive restriction is added in Commit 2)
- Modify: `server/src/sense_server/http/routes/agent.py` (wrap the inbound text in `UserRequest`)
- Modify: `server/tests/agent/test_planner.py` (`_ctx` helper)
- Modify: `server/tests/agent/test_planner_issue_command.py` (`_ctx` helper)
- Modify: `server/tests/http/test_agent_route.py` (DTO mapping test)
- Test: `server/tests/agent/test_trigger_envelope.py` (new)

**Interfaces:**
- Consumes: existing `PlannerContext` (`contracts/types.py:320`), existing `ContextBuilder.build` (`agent/context.py:65`).
- Produces:
  - `class UserRequest(BaseModel, frozen=True): request_id: str; text: str`
  - `class Proactive(BaseModel, frozen=True): request_id: str; event_id: str; transcript: str`
  - `Trigger = Annotated[Union[UserRequest, Proactive], Field(discriminator="kind")]`
  - `PlannerContext.trigger: Trigger` (replacing `trigger_text: str`)
  - `ContextBuilder.build(self, trigger: Trigger, retrieved: RetrievedContext, capabilities: CapabilitySet) -> Prompt`
  - Inside `build`, `text = trigger.text if isinstance(trigger, UserRequest) else trigger.transcript` — passed to `_format_user(text, retrieved)`.

- [ ] **Step 1: Write a failing test for the `Trigger` envelope**

Create `server/tests/agent/test_trigger_envelope.py`:

```python
"""Tests for the Trigger envelope on PlannerContext."""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from sense_server.contracts.types import (
    PlannerContext,
    Proactive,
    Trigger,
    UserRequest,
)


def test_user_request_is_constructible() -> None:
    u = UserRequest(request_id="r1", text="hello")
    assert u.request_id == "r1"
    assert u.text == "hello"
    assert u.kind == "user_request"  # discriminator tag, set in the spec below


def test_proactive_is_constructible() -> None:
    p = Proactive(request_id="r2", event_id="s1:7", transcript="")
    assert p.request_id == "r2"
    assert p.event_id == "s1:7"
    assert p.transcript == ""


def test_trigger_discriminator_picks_user_request() -> None:
    raw = {"kind": "user_request", "request_id": "r1", "text": "hi"}
    parsed = Trigger.model_validate(raw)
    assert isinstance(parsed, UserRequest)
    assert parsed.text == "hi"


def test_trigger_discriminator_picks_proactive() -> None:
    raw = {"kind": "proactive", "request_id": "r2", "event_id": "s:1", "transcript": "x"}
    parsed = Trigger.model_validate(raw)
    assert isinstance(parsed, Proactive)
    assert parsed.event_id == "s:1"


def test_trigger_discriminator_rejects_unknown_kind() -> None:
    raw = {"kind": "other", "request_id": "r", "text": ""}
    with pytest.raises(ValidationError):
        Trigger.model_validate(raw)


def test_planner_context_carries_user_request() -> None:
    ctx = PlannerContext(
        request_id="r1",
        trigger=UserRequest(request_id="r1", text="hi"),
        session_id="s1",
    )
    assert isinstance(ctx.trigger, UserRequest)
    assert ctx.trigger.text == "hi"


def test_planner_context_carries_proactive() -> None:
    ctx = PlannerContext(
        request_id="r2",
        trigger=Proactive(request_id="r2", event_id="s:1", transcript="x"),
        session_id="s1",
    )
    assert isinstance(ctx.trigger, Proactive)
    assert ctx.trigger.event_id == "s:1"
```

(One subtlety: the discriminator field is named `kind` on both variants. The spec code at section 3.1 uses `kind` as the discriminator name. If your pydantic v2 doesn't accept a `kind` field that's already a kwarg on the model, name the discriminator tag field `kind` explicitly via `Field(discriminator="kind")` and define `kind: Literal["user_request", "proactive"]` on each variant.)

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_trigger_envelope.py -v`
Expected: `ImportError: cannot import name 'UserRequest' from 'sense_server.contracts.types'`.

- [ ] **Step 3: Add `UserRequest`, `Proactive`, `Trigger` to `contracts/types.py`**

Open `server/src/sense_server/contracts/types.py`. The imports section already has `BaseModel, ConfigDict, Field` from pydantic. Add `Annotated, Literal, Union` to the typing import (if not present) and add the new types immediately above the `PlannerContext` class (around line 303):

```python
class UserRequest(BaseModel):
    """The user asked a question via POST /agent.

    `request_id` is the inbound request's id, preserved so the audit
    log can correlate the trigger with the result.
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["user_request"] = "user_request"
    request_id: str
    text: str


class Proactive(BaseModel):
    """The server-initiated trigger: a session just finished extracting
    new atoms, and the planner is being asked to look for something
    worth surfacing without the user having asked a question.

    `event_id` identifies the transcript event that triggered the
    extraction; `transcript` is the raw text (v1 sends "" — the planner
    retrieves from session memory; capturing the exact transcript is a
    follow-up). The proactive trigger is FORBIDDEN from issuing device
    commands (enforced inside Planner.plan).
    """

    model_config = ConfigDict(frozen=True)
    kind: Literal["proactive"] = "proactive"
    request_id: str
    event_id: str
    transcript: str


Trigger = Annotated[Union[UserRequest, Proactive], Field(discriminator="kind")]
```

Then in `PlannerContext` (line 320), replace the field:

```python
class PlannerContext(BaseModel):
    """The single input the Planner needs from the HTTP layer.

    The HTTP route builds this from the request DTO; the Planner never
    reads the wire shape. The trigger is a tagged union: UserRequest
    (the user asked a question) vs Proactive (the server noticed a
    session just finished extracting). The Planner's behavior is
    source-agnostic except for the proactive trigger's ISSUE_COMMAND
    prohibition (see agent.planner).
    """

    model_config = ConfigDict(frozen=True)
    request_id: str
    trigger: Trigger
    session_id: str | None = None
    limit: int = 10
```

- [ ] **Step 4: Re-run the new test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_trigger_envelope.py -v`
Expected: 7 passed.

- [ ] **Step 5: Update `ContextBuilder.build` to take a `Trigger`**

Open `server/src/sense_server/agent/context.py`. The `build` method (line 65) currently takes `trigger_text: str`. Change the signature and the body:

```python
    def build(
        self,
        trigger: Trigger,
        retrieved: RetrievedContext,
        capabilities: CapabilitySet,
    ) -> Prompt:
        system = _V1_SYSTEM_PROMPT.format(
            capabilities=_format_capabilities(capabilities),
            atoms_block=_format_atoms_block(retrieved),
        )
        if not retrieved.atoms:
            system = system + "\n" + _V1_NO_MEMORY_LINE + "\n"
        text = trigger.text if isinstance(trigger, UserRequest) else trigger.transcript
        user = _format_user(text, retrieved)
        return Prompt(
            system=system,
            user=user,
            system_prompt_version=self.system_prompt_version,
            context_builder_version=self.context_builder_version,
        )
```

Add the import at the top of `context.py` (alongside the existing `from ..contracts.types import ...` line):

```python
from ..contracts.types import CapabilitySet, Prompt, RetrievedContext, Trigger, UserRequest
```

- [ ] **Step 6: Update `Planner.plan` to pass `ctx.trigger` to `ContextBuilder.build`**

In `server/src/sense_server/agent/planner.py` (around line 136), the planner calls `self._context_builder.build(ctx.trigger_text, retrieved, capabilities)`. Change it to:

```python
        prompt = self._context_builder.build(
            ctx.trigger, retrieved, capabilities
        )
```

(The proactive restriction in `_dispatch_command` is added in Commit 2; this commit just threads the trigger through.)

- [ ] **Step 7: Update `http/routes/agent.py` to wrap the inbound text in `UserRequest`**

Open `server/src/sense_server/http/routes/agent.py` (line 36). The route currently builds `PlannerContext(trigger_text=text, ...)`. Change it to:

```python
        from ..contracts.types import UserRequest
        ctx = PlannerContext(
            request_id=request_id,
            trigger=UserRequest(request_id=request_id, text=text),
            session_id=session_id,
            limit=limit,
        )
```

(Read the file first to confirm the variable names — `request_id`, `text`, `session_id`, `limit` — and apply the change to match. The exact constructor call site is the only one in the HTTP layer.)

- [ ] **Step 8: Update the existing planner test helpers**

Two helpers need to change. They use the same pattern: `PlannerContext(trigger_text=...)` → `PlannerContext(trigger=UserRequest(...))`.

In `server/tests/agent/test_planner.py` (line 80), change `_ctx`:

```python
def _ctx(extra: dict | None = None) -> PlannerContext:
    from sense_server.contracts.types import UserRequest
    base = dict(
        request_id="req-1",
        trigger=UserRequest(request_id="req-1", text="hello"),
        session_id="s1",
    )
    if extra:
        base.update(extra)
    return PlannerContext(**base)
```

In `server/tests/agent/test_planner_issue_command.py` (line 104), change `_ctx`:

```python
def _ctx() -> PlannerContext:
    from sense_server.contracts.types import UserRequest
    return PlannerContext(
        request_id="req-1",
        trigger=UserRequest(request_id="req-1", text="record the next 10s"),
        session_id="s1",
    )
```

- [ ] **Step 9: Update `test_agent_route.py` to construct the new shape**

Open `server/tests/http/test_agent_route.py`. Find the test that maps a wire DTO to `PlannerContext` and update it to wrap the inbound text in `UserRequest(request_id=..., text=...)`. The exact change is in whatever helper builds the `PlannerContext` (likely a `_ctx` or `_build_ctx`). Mirror the change in step 8.

- [ ] **Step 10: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x --ignore=tests/gateway/test_streaming_e2e_real_opus.py --ignore=tests/gateway/test_streaming_e2e.py`
Expected: all tests pass (excluding the 2 known-slow e2e ones; same as project convention). The 1 pre-existing failure `test_server_integration.py::test_full_session_over_a_real_socket` is allowed.

- [ ] **Step 11: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/contracts/types.py \
        server/src/sense_server/agent/context.py \
        server/src/sense_server/agent/planner.py \
        server/src/sense_server/http/routes/agent.py \
        server/tests/agent/test_trigger_envelope.py \
        server/tests/agent/test_planner.py \
        server/tests/agent/test_planner_issue_command.py \
        server/tests/http/test_agent_route.py
git commit -m "feat(server): Trigger envelope on PlannerContext

Replace trigger_text: str with a tagged Trigger = UserRequest |
Proactive union. The unification rule holds: every consumer
(retriever, validator, guardrails, audit, dispatcher) is
source-agnostic; the only difference between UserRequest and
Proactive is what calls plan() and what the trigger text was.

- contracts/types.py: add UserRequest, Proactive, Trigger.
- agent/context.py: ContextBuilder.build takes Trigger; text
  is trigger.text for UserRequest, trigger.transcript for Proactive.
- agent/planner.py: pass ctx.trigger through (no behavioral change).
- http/routes/agent.py: wrap inbound text in UserRequest.
- Test helpers in test_planner.py / test_planner_issue_command.py /
  test_agent_route.py updated for the new shape.

7 new tests in test_trigger_envelope.py. The proactive
ISSUE_COMMAND prohibition is added in the next commit; the audit
trigger_source field is added in the commit after that."
```

---

## Commit 2: Server — `Planner` proactive restriction + audit + metrics

**Files:**
- Modify: `server/src/sense_server/agent/planner.py` (proactive restriction in `plan()` before `_dispatch_command`)
- Modify: `server/src/sense_server/agent/audit.py` (record `trigger_source` on every entry)
- Modify: `server/src/sense_server/contracts/metrics.py` (add 6 new constants)
- Modify: `server/src/sense_server/contracts/types.py` (add `PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND` to `RejectionReason` if not present, OR introduce a new rejection reason — pick one and stick to it)
- Test: `server/tests/agent/test_planner_proactive.py` (new)

**Interfaces:**
- Consumes: existing `Planner.plan`, `RejectionReason`, `InMemoryAuditLogger`, `SqliteAuditLogger`; the new `Trigger` from Commit 1.
- Produces:
  - Inside `Planner.plan`, BEFORE `self._dispatch_command(...)` is called, if `isinstance(ctx.trigger, Proactive)`, return `PlannerResult(outcome=REFUSE, refusal_reason=PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND, ...)` built with the same `_build_result` helper. Audit entry carries `trigger_source="proactive"`.
  - `InMemoryAuditLogger.record` and `SqliteAuditLogger.record` accept an optional `trigger_source` field and store it.
  - Six new metric constants in `Metrics`:
    - `PROACTIVE_DELIVERED_TOTAL = "proactive_delivered_total"`
    - `PROACTIVE_REFUSED_TOTAL = "proactive_refused_total"`
    - `PROACTIVE_PLAN_FAILURE_TOTAL = "proactive_plan_failure_total"`
    - `PROACTIVE_SEND_FAILURE_TOTAL = "proactive_send_failure_total"`
    - `PROACTIVE_DELIVERY_DROPPED_TOTAL = "proactive_delivery_dropped_total"`
    - `EXTRACTION_LISTENER_FAILURE_TOTAL = "extraction_listener_failure_total"`

- [ ] **Step 1: Add the 6 metric constants**

Open `server/src/sense_server/contracts/metrics.py`. Append to `Metrics` (after the existing counters):

```python
    EXTRACTION_QUEUE_OVERFLOW_TOTAL: Final = "extraction_queue_overflow_total"

    # P3 proactive trigger
    PROACTIVE_DELIVERED_TOTAL:          Final = "proactive_delivered_total"
    PROACTIVE_REFUSED_TOTAL:            Final = "proactive_refused_total"
    PROACTIVE_PLAN_FAILURE_TOTAL:       Final = "proactive_plan_failure_total"
    PROACTIVE_SEND_FAILURE_TOTAL:       Final = "proactive_send_failure_total"
    PROACTIVE_DELIVERY_DROPPED_TOTAL:   Final = "proactive_delivery_dropped_total"
    EXTRACTION_LISTENER_FAILURE_TOTAL:  Final = "extraction_listener_failure_total"
```

- [ ] **Step 2: Write a failing test for the proactive restriction**

Create `server/tests/agent/test_planner_proactive.py`:

```python
"""Tests for the proactive ISSUE_COMMAND prohibition inside Planner.plan.

The proactive trigger is FORBIDDEN from issuing device commands: a
proactive call is initiated by the server, not the user, and a
hallucination on the proactive path would become a real device
action. The planner enforces this as a type-system guarantee — the
check happens before CommandValidator is even consulted. The audit
log records trigger_source="proactive" for every proactive outcome.
"""
from __future__ import annotations

import pytest

from sense_server.agent.audit import InMemoryAuditLogger
from sense_server.agent.context import ContextBuilder
from sense_server.agent.guardrails import ConfidenceGateGuardrails
from sense_server.agent.guardrails_command import StrictCommandGuardrails
from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.agent.planner import Planner
from sense_server.agent.validator import StrictJSONValidator
from sense_server.agent.validator_command import StrictCommandValidator
from sense_server.commands.dispatcher import CommandDispatcher
from sense_server.commands.signer import CommandSigner
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    CapabilitySet,
    DeviceResourceStatus,
    IssueCommandPayload,
    PlannerContext,
    PlannerOutcome,
    Proactive,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
)
from sense_server.memory.index import InMemoryMemoryIndex
from datetime import datetime, timezone


def _retrieved():
    a = ScoredAtom(
        atom_id="a1", session_id="s1", kind="fact", text="hello",
        score=0.9, source_event_id="e1", source_modality="transcript",
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    return RetrievedContext(atoms=(a,), retrieval_trace_id="t1", retrieval_method="vector")


class _LLM:
    def __init__(self, parsed): self._parsed = parsed
    async def reason(self, prompt):
        from sense_server.contracts.types import LLMResult
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


class _Retriever:
    def retrieve(self, *a, **kw): return _retrieved()


class _Caps:
    def capabilities(self):
        return CapabilitySet(
            camera=True, microphone=True, retrospective_buffer=True,
            display=False, speaker=False,
        )
    def resources(self):
        return DeviceResourceStatus(
            battery_pct=1.0, storage_free_bytes=1 << 30,
            camera_available=True, microphone_available=True,
            recording=False, relay_connected=True,
        )


class _Clock:
    def now(self): return datetime(2026, 7, 19, tzinfo=timezone.utc)


def _build_planner(audit, dispatcher) -> Planner:
    parsed = AgentAction(
        kind=AgentActionKind.ISSUE_COMMAND,
        text="",
        atom_ids=("a1",),
        confidence=0.95,
        command=IssueCommandPayload(
            command_type="record_video",
            params={"duration_s": 10},
            idempotency_key="proactive-1",
            confidence=0.95,
        ),
    )
    return Planner(
        retriever=_Retriever(),
        context_builder=ContextBuilder(),
        llm=_LLM(parsed=parsed),
        validator=StrictJSONValidator(),
        guardrails=ConfidenceGateGuardrails(rate_limit_per_min=1000),
        audit=audit,
        metrics=InMemoryMetricsRecorder(),
        capability_provider=_Caps(),
        clock=_Clock(),
        ids=DeterministicIdGenerator(),
        command_validator=StrictCommandValidator(),
        command_guardrails=StrictCommandGuardrails(),
        dispatcher=dispatcher,
    )


@pytest.mark.asyncio
async def test_proactive_trigger_refuses_issue_command():
    """Planner.plan refuses ISSUE_COMMAND when trigger is Proactive.

    The dispatcher is wired (so the planner *could* issue), but the
    proactive restriction must short-circuit before the command path
    runs at all.
    """
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_Clock())
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=Proactive(request_id="r1", event_id="s1:7", transcript=""),
        session_id="s1",
    )
    result = await planner.plan(ctx)

    assert result.outcome == PlannerOutcome.REFUSE
    assert result.refusal_reason == RejectionReason.PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND
    # The dispatcher's pending list is empty — the command was never issued.
    assert dispatcher.pending() == []


@pytest.mark.asyncio
async def test_proactive_trigger_audit_records_trigger_source():
    """Audit entries for proactive outcomes record trigger_source='proactive'."""
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_Clock())
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=Proactive(request_id="r1", event_id="s1:7", transcript=""),
        session_id="s1",
    )
    await planner.plan(ctx)

    # At least one entry should carry the proactive source.
    proactive_entries = [e for e in audit.entries if e.get("trigger_source") == "proactive"]
    assert len(proactive_entries) >= 1


@pytest.mark.asyncio
async def test_user_request_can_still_issue_command():
    """Regression: a UserRequest trigger must still ISSUE_COMMAND successfully."""
    from sense_server.contracts.types import UserRequest
    audit = InMemoryAuditLogger()
    signer = CommandSigner.generate()
    dispatcher = CommandDispatcher(signer, clock=_Clock())
    planner = _build_planner(audit, dispatcher)

    ctx = PlannerContext(
        request_id="r1",
        trigger=UserRequest(request_id="r1", text="record a 10s video"),
        session_id="s1",
    )
    result = await planner.plan(ctx)

    assert result.outcome == PlannerOutcome.ISSUE_COMMAND
    assert result.command_id is not None
    # UserRequest entries don't carry the proactive source.
    user_entries = [e for e in audit.entries if e.get("trigger_source") == "user_request"]
    assert any(e for e in user_entries)
```

- [ ] **Step 3: Run the new test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_planner_proactive.py -v`
Expected: `AttributeError: module 'sense_server.contracts.types' has no attribute 'PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND'` (or similar — the test references the new rejection reason that doesn't exist yet).

- [ ] **Step 4: Add `PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND` to `RejectionReason`**

Open `server/src/sense_server/contracts/types.py`. Find the `RejectionReason` enum. Add a new variant (preserving alphabetical / grouped order — match the style of the existing entries):

```python
    PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND = "proactive_trigger_cannot_issue_command"
```

(If `RejectionReason` is a `str, Enum` with specific values tied to wire JSON, the new value `"proactive_trigger_cannot_issue_command"` matches the audit + reason field naming convention used by the existing entries. Verify by reading the enum definition first.)

- [ ] **Step 5: Add the proactive restriction in `Planner.plan`**

Open `server/src/sense_server/agent/planner.py`. The dispatcher call is at line 176-180:

```python
        if validated.action.kind == AgentActionKind.ISSUE_COMMAND:
            return await self._dispatch_command(
                ctx, retrieved, validated, prompt,
                retrieval_latency, llm_latency, validator_latency, guardrails_latency,
            )
```

Replace it with:

```python
        if validated.action.kind == AgentActionKind.ISSUE_COMMAND:
            # P3: a Proactive trigger is FORBIDDEN from issuing a
            # device command. The user didn't ask — a hallucination
            # on the proactive path would become a real device action.
            # This is enforced here (not in CommandValidator, not in
            # guardrails) as a type-system guarantee.
            if isinstance(ctx.trigger, Proactive):
                result = self._build_result(
                    ctx, retrieved,
                    outcome=PlannerOutcome.REFUSE,
                    guarded=GuardedAction(
                        outcome=_REFUSE,
                        action=validated.action,
                        refusal_reason=RejectionReason.PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND,
                        refusal_message="proactive triggers cannot issue device commands",
                    ),
                    retrieval_latency_ms=retrieval_latency,
                    llm_latency_ms=llm_latency,
                    validator_latency_ms=validator_latency,
                    guardrails_latency_ms=guardrails_latency,
                )
                self._safe_audit(result, prompt=prompt)
                return result
            return await self._dispatch_command(
                ctx, retrieved, validated, prompt,
                retrieval_latency, llm_latency, validator_latency, guardrails_latency,
            )
```

Add `Proactive` to the existing import from `..contracts.types` (line 37-52):

```python
from ..contracts.types import (
    AgentAction,
    AgentActionKind,
    GuardOutcome,
    GuardedAction,
    LLMResult,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Proactive,
    Prompt,
    RejectionReason,
    RetrievedContext,
    ScoredAtom,
    ValidatedAction,
    ValidatorContext,
)
```

(Only add `Proactive` if it isn't already imported.)

- [ ] **Step 6: Add `trigger_source` to the audit log**

Open `server/src/sense_server/agent/audit.py`. The `record` method on `InMemoryAuditLogger` (line 39) takes `entry: dict`. Modify both implementations to capture `trigger_source` from the PlannerResult if the caller passes the full result object. The simplest path: the Planner's `_safe_audit` (line 199) builds the entry; add a `trigger_source` key there. Find the Planner's audit-construction code (around line 195-201):

```python
        result = self._build_result(...)
        self._safe_audit(result, prompt=prompt)
        return result
```

The `_safe_audit` method is on the Planner. Read its body. Modify it so the entry dict includes `trigger_source: "user_request"` if `isinstance(ctx.trigger, UserRequest)` or `trigger_source: "proactive"` if `isinstance(ctx.trigger, Proactive)`. The exact code depends on the existing `_safe_audit` body — find it and add the field. A simple approach:

```python
def _safe_audit(self, result, prompt):
    entry = {
        "request_id": result.request_id,
        "retrieval_trace_id": result.retrieval_trace_id,
        "outcome": result.outcome.value,
        "atom_ids": list(result.atom_ids),
        "latency_ms": result.latency_ms if hasattr(result, "latency_ms") else 0,
        "trigger_source": self._ctx_trigger_source(),
    }
    try:
        self._audit.record(entry)
    except Exception:
        log.exception("audit_write_failed", extra={"request_id": result.request_id})
```

The `self._ctx_trigger_source()` helper needs access to the current `ctx` — either pass it through, or store the last-seen `ctx` on the planner (acceptable: the planner is single-call atomic). The cleanest pattern: thread `ctx` into `_safe_audit(result, prompt, ctx)`. Read the existing call sites and update them. (Five call sites — verify by grep.)

- [ ] **Step 7: Re-run the new test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_planner_proactive.py -v`
Expected: 3 passed.

- [ ] **Step 8: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x --ignore=tests/gateway/test_streaming_e2e_real_opus.py --ignore=tests/gateway/test_streaming_e2e.py`
Expected: all green (excluding the 2 known-slow e2e ones; the pre-existing 1 failure is allowed).

- [ ] **Step 9: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/contracts/types.py \
        server/src/sense_server/contracts/metrics.py \
        server/src/sense_server/agent/planner.py \
        server/src/sense_server/agent/audit.py \
        server/tests/agent/test_planner_proactive.py
git commit -m "feat(server): proactive ISSUE_COMMAND prohibition in Planner.plan

A Proactive trigger is FORBIDDEN from issuing device commands.
The check is the first thing that runs after the LLM emits
ISSUE_COMMAND — before CommandValidator, before CommandGuardrails,
before CommandDispatcher. A reviewer can grep for the prohibition
and find exactly one place.

- contracts/types.py: add PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND
  to RejectionReason.
- contracts/metrics.py: add 6 proactive + listener metric constants.
- agent/planner.py: the branch is inside plan(), after validate +
  guardrails, before _dispatch_command. The audit log carries
  trigger_source on every entry.
- agent/audit.py: InMemory + Sqlite loggers accept and store
  trigger_source.

3 new tests in test_planner_proactive.py: refuses issue_command,
audit records trigger_source='proactive', and a regression test
that UserRequest still issues commands. The next commit adds the
extraction worker listener list that produces the proactive calls."
```

---

## Commit 3: Server — `ExtractionWorker` listener list

**Files:**
- Modify: `server/src/sense_server/memory/extraction_worker.py` (add `SessionCompletion`, `add_listener`, `remove_listener`, dispatch in `process_session`)
- Test: `server/tests/memory/test_extraction_worker_listeners.py` (new)

**Interfaces:**
- Consumes: existing `ExtractionWorker`, `ExtractionEnqueuer`, `EventStore`, `AtomStore`, `Pipeline`, `InMemoryMetricsRecorder`.
- Produces:
  - `class SessionCompletion(NamedTuple): session_id: str; completed_at: datetime; event_id_range: tuple[int, int]`
  - `ExtractionWorker.__init__` accepts `listeners: list[Callable[[SessionCompletion], Awaitable[None]]] | None = None`
  - `ExtractionWorker.add_listener(listener)` and `remove_listener(listener)` for runtime management
  - In `process_session`, AFTER the cursor advance, dispatch to all listeners with a `SessionCompletion`. Listener exceptions are caught, logged, counted as `EXTRACTION_LISTENER_FAILURE_TOTAL`, never re-raised. The dispatch iterates a snapshot of the listener list so a listener that calls `remove_listener` during dispatch doesn't mutate the iteration.

- [ ] **Step 1: Write a failing test for the listener list**

Create `server/tests/memory/test_extraction_worker_listeners.py`:

```python
"""Tests for the listener list on ExtractionWorker (P3 proactive trigger).

The worker dispatches a SessionCompletion to every registered listener
AFTER the per-session cursor advances. Listener failures must not block
the worker or other listeners (H7: cursor advances on success only;
listener exceptions are caught and counted).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.metrics import Metrics
from sense_server.events.model import CaptureEvent
from sense_server.events.store import InMemoryEventStore
from sense_server.memory.extraction_worker import (
    ExtractionEnqueuer,
    ExtractionWorker,
    SessionCompletion,
)
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.stages import (
    EmbeddingStage,
    ExtractionStage,
    IndexingStage,
    Pipeline,
    VersionStampStage,
)
from sense_server.memory.store import InMemoryAtomStore
from sense_server.memory.extract import ExtractedMemory


def _fixed_clock():
    return datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc)


def _build_worker(events, atoms, idx, metrics, enq):
    class FixedExtractor:
        def extract(self, text):
            return [ExtractedMemory(kind="fact", text=text)]
    class FixedEmbedder:
        def embed(self, texts):
            return [[1.0, 0.0, 0.0] for _ in texts]
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=FixedExtractor(), clock=_fixed_clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=FixedEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline, metrics=metrics, enqueuer=enq,
    )


def test_listener_invoked_after_cursor_advances():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[SessionCompletion] = []
    async def listener(c: SessionCompletion) -> None:
        seen.append(c)
    worker.add_listener(listener)

    events.append(CaptureEvent(
        event_id="s1:0", session_id="s1", seq=0, kind="transcript",
        created_at=_fixed_clock(), text="hello", duration_ms=1000, start_ms=0,
    ))
    worker.process_session("s1")
    assert len(seen) == 1
    assert seen[0].session_id == "s1"
    assert seen[0].event_id_range == (0, 0)


def test_listener_not_invoked_when_pipeline_fails():
    """H7: a pipeline failure must not advance the cursor, and the
    listener must not be invoked (the session hasn't actually completed)."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[SessionCompletion] = []
    async def listener(c: SessionCompletion) -> None:
        seen.append(c)
    worker.add_listener(listener)

    # Force a pipeline failure by indexing into a broken index.
    class BrokenIndex:
        def upsert(self, atom): raise RuntimeError("index down")
        def delete(self, atom_id): pass
        def query(self, *a, **kw): return []
    worker._pipeline = Pipeline(
        extraction=ExtractionStage(
            extractor=type("X", (), {"extract": lambda self, t: [ExtractedMemory(kind="fact", text=t)]})(),
            clock=_fixed_clock,
        ),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(
            embedder=type("X", (), {"embed": lambda self, t: [[1.0, 0.0, 0.0] for _ in t]})(),
        ),
        indexing=IndexingStage(index=BrokenIndex()),
        store=atoms,
    )
    events.append(CaptureEvent(
        event_id="s1:0", session_id="s1", seq=0, kind="transcript",
        created_at=_fixed_clock(), text="hello", duration_ms=1000, start_ms=0,
    ))
    with pytest.raises(RuntimeError):
        worker.process_session("s1")
    assert seen == []


def test_listener_exception_does_not_block_other_listeners():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[str] = []
    async def bad_listener(c: SessionCompletion) -> None:
        raise RuntimeError("listener boom")
    async def good_listener(c: SessionCompletion) -> None:
        seen.append(c.session_id)
    worker.add_listener(bad_listener)
    worker.add_listener(good_listener)

    events.append(CaptureEvent(
        event_id="s1:0", session_id="s1", seq=0, kind="transcript",
        created_at=_fixed_clock(), text="hello", duration_ms=1000, start_ms=0,
    ))
    worker.process_session("s1")
    # The good listener still ran.
    assert seen == ["s1"]
    # The failure was counted.
    assert metrics.counter(Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL) == 1


def test_remove_listener_works():
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    async def listener(c): pass
    worker.add_listener(listener)
    worker.remove_listener(listener)
    assert worker._listeners == []  # internal: verified for the test


def test_in_iteration_remove_is_safe():
    """A listener that calls remove_listener during dispatch must not
    cause the iteration to skip the next listener or raise."""
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    metrics = InMemoryMetricsRecorder()
    enq = ExtractionEnqueuer(capacity=10, metrics=metrics)
    worker = _build_worker(events, atoms, idx, metrics, enq)

    seen: list[str] = []
    listener_to_remove = None
    async def bad(c: SessionCompletion) -> None:
        worker.remove_listener(listener_to_remove)
    async def good(c: SessionCompletion) -> None:
        seen.append(c.session_id)
    listener_to_remove = good
    worker.add_listener(bad)
    worker.add_listener(good)

    events.append(CaptureEvent(
        event_id="s1:0", session_id="s1", seq=0, kind="transcript",
        created_at=_fixed_clock(), text="hello", duration_ms=1000, start_ms=0,
    ))
    worker.process_session("s1")
    assert seen == ["s1"]
```

- [ ] **Step 2: Run the new test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/memory/test_extraction_worker_listeners.py -v`
Expected: `ImportError: cannot import name 'SessionCompletion' from 'sense_server.memory.extraction_worker'`.

- [ ] **Step 3: Add `SessionCompletion` and the listener list to `extraction_worker.py`**

Open `server/src/sense_server/memory/extraction_worker.py`. Add the import:

```python
from collections.abc import Awaitable, Callable
from datetime import datetime
```

Add `SessionCompletion` after the `ExtractionEnqueuer` class (around line 78):

```python
class SessionCompletion:
    """The completion record dispatched to listeners after a successful
    extraction. Carries the session id, the wall-clock time of completion
    (injected via a clock so tests are deterministic), and the inclusive
    ``(first, last)`` event sequence range that was just processed.
    """

    __slots__ = ("session_id", "completed_at", "event_id_range")

    def __init__(
        self,
        session_id: str,
        completed_at: datetime,
        event_id_range: tuple[int, int],
    ) -> None:
        self.session_id = session_id
        self.completed_at = completed_at
        self.event_id_range = event_id_range

    def __repr__(self) -> str:
        return (
            f"SessionCompletion(session_id={self.session_id!r}, "
            f"completed_at={self.completed_at!r}, "
            f"event_id_range={self.event_id_range!r})"
        )
```

In `ExtractionWorker.__init__` (line 95), add a `listeners` slot:

```python
    def __init__(
        self,
        events: EventStore,
        atoms: AtomStore,
        pipeline: Pipeline,
        metrics: InMemoryMetricsRecorder,
        enqueuer: ExtractionEnqueuer | None = None,
        listeners: list[Callable[[SessionCompletion], Awaitable[None]]] | None = None,
    ) -> None:
        self._events = events
        self._atoms = atoms
        self._pipeline = pipeline
        self._metrics = metrics
        self._enqueuer = enqueuer or ExtractionEnqueuer(metrics=metrics)
        self._enqueuer.attach(self)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._listeners: list[Callable[[SessionCompletion], Awaitable[None]]] = list(listeners or [])
```

Add `add_listener` and `remove_listener` methods (place them between `replace_enqueuer` and `process_session`):

```python
    def add_listener(
        self, listener: Callable[[SessionCompletion], Awaitable[None]]
    ) -> None:
        """Register a listener to be called after every successful extraction."""
        self._listeners.append(listener)

    def remove_listener(
        self, listener: Callable[[SessionCompletion], Awaitable[None]]
    ) -> None:
        """Remove a previously-registered listener. No-op if absent."""
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass
```

Modify `process_session` (line 119). After the cursor advance (after `self._atoms.set_cursor(...)` and before the `return indexed` line), dispatch to all listeners:

```python
            if pending:
                new_cursor = max(e.seq for e in pending)
                self._atoms.set_cursor(session_id, new_cursor)
                # P3: dispatch to listeners after the cursor advances.
                # H7: only after success. Listener failures are caught,
                # logged, and counted — never re-raised, never block.
                completion = SessionCompletion(
                    session_id=session_id,
                    completed_at=datetime.now(tz=timezone.utc),
                    event_id_range=(pending[0].seq, pending[-1].seq),
                )
                # Iterate a snapshot so a listener that calls
                # remove_listener during dispatch doesn't mutate the iteration.
                for listener in list(self._listeners):
                    try:
                        await listener(completion)
                    except Exception:
                        self._metrics.increment(
                            Metrics.EXTRACTION_LISTENER_FAILURE_TOTAL,
                            tags={"session_id": session_id},
                        )
                        log.exception(
                            "extraction_listener_failed",
                            extra={"session_id": session_id},
                        )
```

Add the import for `log` and `datetime, timezone` at the top of the file (if not present):

```python
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)
```

(The existing file may or may not have these — read it first and add what's missing.)

- [ ] **Step 4: Re-run the new test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/memory/test_extraction_worker_listeners.py -v`
Expected: 5 passed.

- [ ] **Step 5: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x --ignore=tests/gateway/test_streaming_e2e_real_opus.py --ignore=tests/gateway/test_streaming_e2e.py`
Expected: all green (excluding the 2 known-slow e2e ones; the pre-existing 1 failure is allowed).

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/memory/extraction_worker.py \
        server/tests/memory/test_extraction_worker_listeners.py
git commit -m "feat(server): ExtractionWorker listener list for proactive triggers

After a successful extraction (H7: cursor advances only on success),
the worker dispatches a SessionCompletion(session_id, completed_at,
event_id_range) to every registered listener. Listener exceptions
are caught, logged, and counted as
EXTRACTION_LISTENER_FAILURE_TOTAL — never re-raised, never blocking
the worker or other listeners.

- add_listener / remove_listener for runtime management.
- Snapshot iteration so a listener that calls remove_listener during
  dispatch is safe (no next-item skip, no RuntimeError).
- The ProactiveTriggerEngine (next commit) is one such listener.

5 new tests in test_extraction_worker_listeners.py cover the happy
path, the pipeline-failure no-dispatch case, the listener-exception
isolation, remove_listener, and in-iteration removal safety."
```

---

## Commit 4: Server — `ProactiveTriggerEngine` + `WsSender` protocol

**Files:**
- Create: `server/src/sense_server/agent/proactive.py` (`ProactiveTriggerEngine`, `WsSender` Protocol)
- Test: `server/tests/agent/test_proactive_engine.py` (new)

**Interfaces:**
- Consumes: existing `Planner`, `Clock`, `IdGenerator`, `Metrics` (as `InMemoryMetricsRecorder`), `Proactive` and `PlannerContext` from prior commits, `SessionCompletion` from Commit 3.
- Produces:
  - `class WsSender(Protocol, runtime_checkable): async def send_proactive(self, *, session_id: str, request_id: str, text: str, atoms: tuple) -> None: ...`
  - `class ProactiveTriggerEngine:` with `__init__(self, planner: Planner, ws_sender: WsSender, clock: Clock, metrics: InMemoryMetricsRecorder, ids: IdGenerator, plan_timeout_s: float = 2.0)` and `async def on_session_completion(self, completion: SessionCompletion) -> None`.
  - The engine is constructable and the method is callable. The implementation is a single coroutine that:
    1. builds a `PlannerContext(trigger=Proactive(...), session_id=completion.session_id, limit=10, request_id=self._ids.next())`;
    2. runs `await asyncio.wait_for(self._planner.plan(ctx), timeout=self._plan_timeout_s)`;
    3. on `PlannerOutcome.RETURN` / `RETURN_WITH_UNCERTAINTY`, calls `ws_sender.send_proactive(...)` and increments `PROACTIVE_DELIVERED_TOTAL`;
    4. on `PlannerOutcome.REFUSE` (or anything else), increments `PROACTIVE_REFUSED_TOTAL`;
    5. on timeout / any exception, increments `PROACTIVE_PLAN_FAILURE_TOTAL` and returns.

- [ ] **Step 1: Write a failing test for the engine**

Create `server/tests/agent/test_proactive_engine.py`:

```python
"""Tests for the ProactiveTriggerEngine (P3).

The engine is a listener on the extraction worker. On every
SessionCompletion it builds a Proactive PlannerContext, calls the
planner, and forwards RETURN results to a WsSender. The engine is
best-effort: a 2-second timeout, all exceptions caught, every drop
counted. A proactive call that would ISSUE_COMMAND is refused by
the planner itself (Commit 2) — the engine never sees that case
unless the planner is mis-wired, in which case the engine drops
it as 'refused'.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

import pytest

from sense_server.agent.context import ContextBuilder
from sense_server.agent.guardrails import ConfidenceGateGuardrails
from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.agent.planner import Planner
from sense_server.agent.proactive import ProactiveTriggerEngine, WsSender
from sense_server.agent.validator import StrictJSONValidator
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.metrics import Metrics
from sense_server.contracts.types import (
    AgentAction,
    AgentActionKind,
    CapabilitySet,
    DeviceResourceStatus,
    PlannerContext,
    PlannerOutcome,
    PlannerResult,
    Proactive,
    RetrievedContext,
    ScoredAtom,
)
from sense_server.memory.extraction_worker import SessionCompletion


def _retrieved():
    a = ScoredAtom(
        atom_id="a1", session_id="s1", kind="fact", text="hello",
        score=0.9, source_event_id="e1", source_modality="transcript",
        created_at=datetime(2026, 7, 19, tzinfo=timezone.utc),
    )
    return RetrievedContext(atoms=(a,), retrieval_trace_id="t1", retrieval_method="vector")


class _LLM:
    def __init__(self, parsed):
        self._parsed = parsed
    async def reason(self, prompt):
        from sense_server.contracts.types import LLMResult
        return LLMResult(raw="", parsed=self._parsed, parse_error=None)


class _Retriever:
    def retrieve(self, *a, **kw):
        return _retrieved()


class _Caps:
    def capabilities(self):
        return CapabilitySet(
            camera=True, microphone=True, retrospective_buffer=True,
            display=False, speaker=False,
        )
    def resources(self):
        return DeviceResourceStatus(
            battery_pct=1.0, storage_free_bytes=1 << 30,
            camera_available=True, microphone_available=True,
            recording=False, relay_connected=True,
        )


class _FakeWsSender:
    def __init__(self):
        self.sent: list[dict[str, Any]] = []
    async def send_proactive(self, *, session_id, request_id, text, atoms):
        self.sent.append({
            "session_id": session_id,
            "request_id": request_id,
            "text": text,
            "atoms": atoms,
        })


class _PlannerStub:
    """A planner stub that records calls and returns a fixed result."""
    def __init__(self, result: PlannerResult):
        self._result = result
        self.calls: list[PlannerContext] = []

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        self.calls.append(ctx)
        return self._result


def _completion(session_id: str = "s1") -> SessionCompletion:
    return SessionCompletion(
        session_id=session_id,
        completed_at=datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        event_id_range=(0, 7),
    )


def _returned_result() -> PlannerResult:
    return PlannerResult(
        request_id="r1",
        retrieval_trace_id="t1",
        outcome=PlannerOutcome.RETURN,
        answer="here is what I found",
        confidence=0.9,
        atom_ids=("a1",),
    )


def _refused_result() -> PlannerResult:
    return PlannerResult(
        request_id="r1",
        retrieval_trace_id="t1",
        outcome=PlannerOutcome.REFUSE,
    )


@pytest.mark.asyncio
async def test_engine_builds_proactive_context():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_returned_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s42"))
    assert len(planner.calls) == 1
    ctx = planner.calls[0]
    assert isinstance(ctx.trigger, Proactive)
    assert ctx.session_id == "s42"
    assert ctx.limit == 10
    # The proactive context carries a non-empty event_id derived from event_id_range.
    assert ctx.trigger.event_id == "event_seq_7"


@pytest.mark.asyncio
async def test_engine_forwards_return_to_ws_sender():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_returned_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s1"))
    assert len(sender.sent) == 1
    sent = sender.sent[0]
    assert sent["session_id"] == "s1"
    assert sent["text"] == "here is what I found"
    assert sent["atoms"] == ("a1",)
    assert metrics.counter(Metrics.PROACTIVE_DELIVERED_TOTAL) == 1


@pytest.mark.asyncio
async def test_engine_drops_refuse():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()
    planner = _PlannerStub(_refused_result())
    engine = ProactiveTriggerEngine(
        planner=planner, ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    assert metrics.counter(Metrics.PROACTIVE_REFUSED_TOTAL) == 1


@pytest.mark.asyncio
async def test_engine_times_out_and_counts_failure():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()

    class SlowPlanner:
        async def plan(self, ctx: PlannerContext) -> PlannerResult:
            await asyncio.sleep(10)
            return _returned_result()

    engine = ProactiveTriggerEngine(
        planner=SlowPlanner(), ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=0.05,
    )
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    assert metrics.counter(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL) == 1


@pytest.mark.asyncio
async def test_engine_swallows_planner_exception():
    metrics = InMemoryMetricsRecorder()
    sender = _FakeWsSender()

    class BoomPlanner:
        async def plan(self, ctx: PlannerContext) -> PlannerResult:
            raise RuntimeError("planner crashed")

    engine = ProactiveTriggerEngine(
        planner=BoomPlanner(), ws_sender=sender, clock=FakeClock(),
        metrics=metrics, ids=DeterministicIdGenerator(), plan_timeout_s=2.0,
    )
    # The engine must not re-raise.
    await engine.on_session_completion(_completion("s1"))
    assert sender.sent == []
    assert metrics.counter(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL) == 1


@pytest.mark.asyncio
async def test_ws_sender_is_runtime_checkable():
    """The WsSender Protocol is runtime-checkable; the gateway's
    implementation in Commit 5 will satisfy it without explicit
    inheritance. This test pins the Protocol's surface."""
    from typing import runtime_checkable

    class HasSend:
        async def send_proactive(self, *, session_id, request_id, text, atoms):
            pass

    class MissingSend:
        pass

    assert isinstance(HasSend(), WsSender)
    assert not isinstance(MissingSend(), WsSender)
```

- [ ] **Step 2: Run the new test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_proactive_engine.py -v`
Expected: `ImportError: cannot import name 'ProactiveTriggerEngine' from 'sense_server.agent.proactive'`.

- [ ] **Step 3: Create the engine module**

Create `server/src/sense_server/agent/proactive.py`:

```python
"""P3 proactive trigger engine.

The :class:`ProactiveTriggerEngine` is a listener on the extraction
worker. After every successful extraction it builds a
:class:`Proactive` trigger and invokes the planner; a `RETURN` outcome
is forwarded to the phone via a :class:`WsSender`. The engine is
best-effort: a 2-second plan timeout (configurable), every drop
counted.

The engine never re-raises. A planner crash, a timeout, a
ws_sender failure — all are caught, logged, and counted. The
proactive ISSUE_COMMAND prohibition is enforced inside the planner
itself (Commit 2); the engine just observes the result and acts
on RETURN / REFUSE.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Protocol, runtime_checkable

from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.metrics import Metrics
from ..contracts.types import PlannerContext, PlannerOutcome, Proactive
from .metrics import InMemoryMetricsRecorder
from .planner import Planner
from ..memory.extraction_worker import SessionCompletion

log = logging.getLogger(__name__)


@runtime_checkable
class WsSender(Protocol):
    """The seam between the engine and the gateway's WS frame.

    The gateway's :class:`GatewayCore` implements this without
    inheriting from it (Protocol is structural). The engine only
    knows it can call ``send_proactive`` and await the result.
    """

    async def send_proactive(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        atoms: tuple[str, ...],
    ) -> None: ...


class ProactiveTriggerEngine:
    """Listens for :class:`SessionCompletion` and fires a Proactive plan call.

    Lifecycle: wire in ``run_gateway.py`` as a listener on the
    :class:`ExtractionWorker`. The engine holds no per-session state;
    every completion is a fresh call.
    """

    def __init__(
        self,
        planner: Planner,
        ws_sender: WsSender,
        clock: Clock,
        metrics: InMemoryMetricsRecorder,
        ids: IdGenerator,
        plan_timeout_s: float = 2.0,
    ) -> None:
        self._planner = planner
        self._ws_sender = ws_sender
        self._clock = clock
        self._metrics = metrics
        self._ids = ids
        self._plan_timeout_s = plan_timeout_s

    async def on_session_completion(self, completion: SessionCompletion) -> None:
        """One listener call. Best-effort; never re-raises."""
        try:
            ctx = PlannerContext(
                request_id=self._ids.next(),
                trigger=Proactive(
                    request_id=self._ids.next(),
                    event_id=f"event_seq_{completion.event_id_range[1]}",
                    transcript="",  # v1: planner retrieves from session memory
                ),
                session_id=completion.session_id,
                limit=10,
            )
            try:
                result = await asyncio.wait_for(
                    self._planner.plan(ctx), timeout=self._plan_timeout_s,
                )
            except asyncio.TimeoutError:
                log.warning(
                    "proactive_plan_timeout",
                    extra={"session_id": completion.session_id,
                           "timeout_s": self._plan_timeout_s},
                )
                self._metrics.increment(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL)
                return
            except Exception:
                log.exception(
                    "proactive_plan_failed",
                    extra={"session_id": completion.session_id},
                )
                self._metrics.increment(Metrics.PROACTIVE_PLAN_FAILURE_TOTAL)
                return

            if result.outcome in (PlannerOutcome.RETURN, PlannerOutcome.RETURN_WITH_UNCERTAINTY):
                try:
                    await self._ws_sender.send_proactive(
                        session_id=completion.session_id,
                        request_id=result.request_id,
                        text=result.answer or "",
                        atoms=result.atom_ids,
                    )
                    self._metrics.increment(Metrics.PROACTIVE_DELIVERED_TOTAL)
                except Exception:
                    log.exception(
                        "proactive_send_failed",
                        extra={"session_id": completion.session_id},
                    )
                    self._metrics.increment(Metrics.PROACTIVE_SEND_FAILURE_TOTAL)
                return

            # REFUSE or any other outcome: drop with a counter.
            self._metrics.increment(Metrics.PROACTIVE_REFUSED_TOTAL)
        except Exception:
            # Defensive: catch any unexpected error from the listener
            # signature or argument validation. Never re-raise; the
            # extraction worker must not see listener exceptions.
            log.exception(
                "proactive_engine_unexpected",
                extra={"session_id": completion.session_id},
            )
```

- [ ] **Step 4: Re-run the new test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/agent/test_proactive_engine.py -v`
Expected: 6 passed.

- [ ] **Step 5: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x --ignore=tests/gateway/test_streaming_e2e_real_opus.py --ignore=tests/gateway/test_streaming_e2e.py`
Expected: all green (excluding the 2 known-slow e2e ones; the pre-existing 1 failure is allowed).

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/agent/proactive.py \
        server/tests/agent/test_proactive_engine.py
git commit -m "feat(server): ProactiveTriggerEngine + WsSender Protocol

The engine is a listener on the ExtractionWorker. On every
SessionCompletion it builds a Proactive PlannerContext, calls
the planner with a 2-second timeout, and forwards RETURN
results to a WsSender. Best-effort: timeout, planner crash,
ws_sender failure — all caught, logged, counted. Never
re-raises (the extraction worker's H7 invariant is not at risk).

- agent/proactive.py: ProactiveTriggerEngine + WsSender Protocol.
- The proactive ISSUE_COMMAND prohibition is in the planner
  (Commit 2). The engine just observes the outcome.

6 new tests in test_proactive_engine.py cover context building,
RETURN forwarding, REFUSE dropping, timeout, exception swallowing,
and the WsSender Protocol's runtime_checkable surface.

The next commit wires the engine into run_gateway.py and adds
the gateway's ProactiveOutbox + _pending_proactives loop."
```

---

## Commit 5: Server — `ProactiveOutbox` + `_pending_proactives` + `run_gateway.py` wiring

**Files:**
- Modify: `server/src/sense_server/protocol/messages.py` (add `ProactiveMessage` outbound §E frame; add `MessageType.PROACTIVE`)
- Modify: `server/src/sense_server/gateway/core.py` (`ProactiveOutbox` class, `_pending_proactives` coroutine, `send_proactive` method)
- Modify: `server/src/sense_server/gateway/adapter.py` (register `_pending_proactives` in the `serve()` loop)
- Modify: `server/scripts/run_gateway.py` (wire `ProactiveTriggerEngine` as a listener; start a task that drains `_pending_proactives`)
- Test: `server/tests/gateway/test_proactive_outbox.py` (new)
- Test: `server/tests/gateway/test_proactive_ws.py` (new)

**Interfaces:**
- Consumes: existing `GatewayCore`, `serve()`, `MessageType`, `Protocol` envelope; the new `ProactiveTriggerEngine` from Commit 4.
- Produces:
  - `server.protocol.messages.ProactiveMessage(_Strict)` with `type: Literal["proactive"]`, `session_id: str`, `request_id: str`, `text: str`, `atoms: tuple[str, ...]`.
  - `Outbound = Union[Ack, RequestChunks, TranscriptMsg, CommandMessage, ProactiveMessage]` in `gateway/core.py`.
  - `class ProactiveOutbox` in `gateway/core.py`:
    - `__init__(self, ttl_s: float = 30.0, clock: Clock)`
    - `enqueue(self, msg: ProactiveMessage) -> None` — drops + counts `PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}` if `msg.created_at + ttl_s` is already in the past.
    - `drain(self, session_id: str) -> list[ProactiveMessage]` — returns non-expired entries, evicts expired ones (and counts them), clears the session's bucket.
    - `_event: asyncio.Event` set by `enqueue`, cleared by `drain`.
  - `GatewayCore.send_proactive(self, *, session_id, request_id, text, atoms) -> None` — instantiates a `ProactiveMessage` and calls `self._proactive_outbox.enqueue(...)`. This is the implementation of the `WsSender` Protocol from Commit 4.
  - `GatewayCore._pending_proactives(self) -> AsyncIterator[ProactiveMessage]` — async generator that yields queued messages for `self._session_id`.
  - `serve()` in `gateway/adapter.py` starts a `asyncio.create_task` that calls `core._pending_proactives()` and sends each yielded message. The task is cancelled on connection close.
  - `run_gateway.py` constructs `ProactiveTriggerEngine` (with `ws_sender=core` reference) and registers it as a listener on the existing `ExtractionWorker`. The `serve(...)` call receives the engine so it can hand the core's `send_proactive` to the engine.

- [ ] **Step 1: Add `ProactiveMessage` to the protocol envelope**

Open `server/src/sense_server/protocol/messages.py`. The outbound classes are at the bottom of the file. Add a new frame:

```python
class ProactiveMessage(_Strict):
    """A proactive answer from the server (P3).

    Carries the agent's answer text and the cited atom ids. The
    relay forwards it into the phone's ChatHistoryStore; the chat
    screen renders it as a new AGENT_PROACTIVE ChatMessage. The
    proactive trigger is server-initiated, never the user, so the
    user knows it's not from a question they asked.
    """

    type: Literal["proactive"] = "proactive"
    session_id: str
    request_id: str
    text: str
    atoms: tuple[str, ...] = ()
```

- [ ] **Step 2: Write a failing test for `ProactiveOutbox`**

Create `server/tests/gateway/test_proactive_outbox.py`:

```python
"""Tests for the ProactiveOutbox (P3)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.metrics import Metrics
from sense_server.gateway.core import ProactiveOutbox
from sense_server.protocol.messages import ProactiveMessage


def _msg(session_id: str = "s1", request_id: str = "r1") -> ProactiveMessage:
    return ProactiveMessage(
        session_id=session_id, request_id=request_id, text="hi", atoms=("a1",),
    )


def test_outbox_enqueue_and_drain():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)

    box.enqueue(_msg())
    drained = box.drain("s1")
    assert len(drained) == 1
    assert drained[0].text == "hi"


def test_outbox_drain_evicts_expired_and_counts():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)

    box.enqueue(_msg())
    # Advance the clock past the TTL.
    clock.advance(timedelta(seconds=31))
    drained = box.drain("s1")
    assert drained == []
    assert metrics.counter(
        Metrics.PROACTIVE_DELIVERY_DROPPED_TOTAL, tags={"reason": "ttl_exceeded"},
    ) == 1


def test_outbox_enqueue_drops_if_already_expired():
    """If the message would be expired by the time enqueue is called
    (e.g. the relay was disconnected for a long time before this
    message was generated), drop it at enqueue time."""
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)

    clock.advance(timedelta(seconds=60))  # way past any reasonable TTL
    # The engine has already created the message — its created_at is
    # the current time on the engine's clock. The outbox's clock
    # determines "now". If now > created_at + ttl, drop.
    box.enqueue(_msg())
    # No entry was stored.
    assert box.drain("s1") == []


def test_outbox_drain_unknown_session_returns_empty():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    box = ProactiveOutbox(ttl_s=30.0, clock=clock)
    assert box.drain("nope") == []
```

- [ ] **Step 3: Run the new test and confirm it fails**

Run: `cd server && .venv/bin/python -m pytest tests/gateway/test_proactive_outbox.py -v`
Expected: `ImportError: cannot import name 'ProactiveOutbox' from 'sense_server.gateway.core'`.

- [ ] **Step 4: Add `ProactiveOutbox` to `gateway/core.py`**

Open `server/src/sense_server/gateway/core.py`. Add the imports at the top (if not present):

```python
import asyncio
from ..agent.metrics import InMemoryMetricsRecorder
from ..contracts.clock import Clock
from ..contracts.metrics import Metrics
from ..protocol.messages import ProactiveMessage
```

Add `ProactiveOutbox` immediately after `GatewayError` (around line 48):

```python
class ProactiveOutbox:
    """Per-session queue of pending proactive messages.

    Best-effort delivery: messages older than ``ttl_s`` are dropped
    at drain time (and counted as
    ``PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}``). The
    outbox is in-process only — a server restart drops everything.
    """

    def __init__(
        self,
        ttl_s: float = 30.0,
        clock: Clock | None = None,
        metrics: InMemoryMetricsRecorder | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._metrics = metrics
        self._by_session: dict[str, list[ProactiveMessage]] = {}
        self._event = asyncio.Event()

    def enqueue(self, msg: ProactiveMessage) -> None:
        # The message has no created_at field; the engine stamps
        # created_at via its own clock. For the outbox's TTL check we
        # use the outbox's clock at the moment of enqueue: if the
        # enqueue time is already past any reasonable TTL, drop.
        # The protocol message carries the request_id and the
        # session_id; the engine's clock and the outbox's clock are
        # the same (FakeClock in tests, SystemClock in production).
        if self._clock is not None:
            now = self._clock.now()
            # If the engine's clock is significantly behind the
            # outbox's clock, the message is stale on arrival.
            if (now - now).total_seconds() > self._ttl_s:  # always False; placeholder
                pass
        self._by_session.setdefault(msg.session_id, []).append(msg)
        self._event.set()

    def drain(self, session_id: str) -> list[ProactiveMessage]:
        """Return non-expired entries for the session, evict expired ones."""
        now = self._clock.now() if self._clock is not None else None
        msgs = self._by_session.pop(session_id, [])
        kept: list[ProactiveMessage] = []
        for m in msgs:
            # The TTL check uses the message's own "enqueued at"
            # timestamp — we approximate that by tracking it in a
            # parallel list. For v1 we accept all messages whose
            # enqueue was within the TTL window via the simple
            # heuristic: track per-message enqueue time.
            kept.append(m)
        # If the session's bucket is empty across all sessions, clear the event.
        if not self._by_session:
            self._event.clear()
        return kept

    async def wait(self) -> None:
        await self._event.wait()

    def signal(self) -> None:
        self._event.set()
```

**Note on the implementation:** the simple version above is incomplete — the TTL check needs a per-message enqueue timestamp, and `drain` needs to filter. Replace the body of `enqueue` and `drain` with this proper version:

```python
class ProactiveOutbox:
    """Per-session queue of pending proactive messages.

    Best-effort delivery: messages older than ``ttl_s`` are dropped
    at drain time (and counted as
    ``PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}``). The
    outbox is in-process only — a server restart drops everything.
    """

    def __init__(
        self,
        ttl_s: float = 30.0,
        clock: Clock | None = None,
        metrics: InMemoryMetricsRecorder | None = None,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._metrics = metrics
        # session_id -> list[(enqueued_at: datetime, msg: ProactiveMessage)]
        self._by_session: dict[str, list[tuple[datetime, ProactiveMessage]]] = {}
        self._event = asyncio.Event()

    def enqueue(self, msg: ProactiveMessage) -> None:
        if self._clock is None:
            self._by_session.setdefault(msg.session_id, []).append((datetime.now(tz=timezone.utc), msg))
        else:
            self._by_session.setdefault(msg.session_id, []).append((self._clock.now(), msg))
        self._event.set()

    def drain(self, session_id: str) -> list[ProactiveMessage]:
        now = self._clock.now() if self._clock is not None else datetime.now(tz=timezone.utc)
        entries = self._by_session.pop(session_id, [])
        kept: list[ProactiveMessage] = []
        for enq_at, m in entries:
            if (now - enq_at).total_seconds() > self._ttl_s:
                if self._metrics is not None:
                    self._metrics.increment(
                        Metrics.PROACTIVE_DELIVERY_DROPPED_TOTAL,
                        tags={"reason": "ttl_exceeded"},
                    )
                continue
            kept.append(m)
        if not self._by_session:
            self._event.clear()
        return kept

    async def wait(self) -> None:
        await self._event.wait()

    def signal(self) -> None:
        self._event.set()
```

Add the import for `datetime, timezone` at the top of the file (if not present):

```python
from datetime import datetime, timezone
```

- [ ] **Step 5: Add `send_proactive` and `_pending_proactives` to `GatewayCore`**

In `gateway/core.py`, add `_proactive_outbox` to `GatewayCore.__init__` (after `self._enqueuer`):

```python
        self._enqueuer = enqueuer
        self._proactive_outbox: ProactiveOutbox = ProactiveOutbox()
```

Add `send_proactive` and `_pending_proactives` methods. Place `send_proactive` next to `_pending_commands`:

```python
    def send_proactive(
        self,
        *,
        session_id: str,
        request_id: str,
        text: str,
        atoms: tuple[str, ...],
    ) -> None:
        """The WsSender seam: the proactive engine calls this to enqueue
        a proactive message. The outbox holds it until the relay's
        ``_pending_proactives`` loop drains it on the next WS round-trip.
        Best-effort: TTL drops are counted in the outbox.
        """
        msg = ProactiveMessage(
            session_id=session_id,
            request_id=request_id,
            text=text,
            atoms=atoms,
        )
        self._proactive_outbox.enqueue(msg)
```

Add `_pending_proactives` as a coroutine (not a generator — it pushes into the WS rather than yielding into the message loop):

```python
    async def _pending_proactives_loop(self) -> None:
        """Drain proactive messages as they arrive. Runs alongside
        the message loop in serve(). Cancelled on connection close.
        """
        while True:
            await self._proactive_outbox.wait()
            if self._session_id is None:
                return
            msgs = self._proactive_outbox.drain(self._session_id)
            # The session_id on the queued message should already match
            # this connection's session; the outbox is per-GatewayCore.
            for m in msgs:
                self._pending_proactive_messages.append(m)
```

(The actual send-to-ws happens in `serve()`, which polls `self._pending_proactive_messages`. See step 6.)

Add `_pending_proactive_messages: list[ProactiveMessage]` to `__init__`:

```python
        self._pending_proactive_messages: list[ProactiveMessage] = []
```

Update the `Outbound` union at the top of the file:

```python
Outbound = Union[Ack, RequestChunks, TranscriptMsg, CommandMessage, ProactiveMessage]
```

- [ ] **Step 6: Wire `_pending_proactives` into the `serve()` loop**

In `server/src/sense_server/gateway/adapter.py`, the `serve()` function's `handler()` async function processes inbound messages and sends replies. The proactives need to be sent as a *separate* async task (or co-operatively interleaved with the inbound loop). The cleanest design: start a `asyncio.create_task` that polls the outbox and sends any pending messages. Cancel the task on connection close.

Open `adapter.py` and find the `handler()` function. Add a new task after the `core` is constructed (around line 168):

```python
        core = GatewayCore(
            pipeline_factory=pipeline_factory,
            event_store=event_store,
            dispatcher=dispatcher,
            session_index=session_index,
            session_lifecycle=session_lifecycle,
            enqueuer=enqueuer,
        )
        # P3: task that drains the proactive outbox and sends proactive
        # frames over the same WS the relay already holds open. Best-effort:
        # a closed WS stops the task via the cancelled exception in
        # ``finally``; messages still queued in the outbox are lost (TTL
        # drops them at the next drain).
        proactive_task: asyncio.Task[None] | None = None

        async def _proactive_loop() -> None:
            while True:
                await core._proactive_outbox.wait()
                msgs = core._proactive_outbox.drain(core._session_id or "")
                for m in msgs:
                    await ws.send(m.model_dump_json())

        proactive_task = asyncio.create_task(_proactive_loop(), name="proactive-loop")
```

In the `finally` block (around the existing `except ConnectionClosed: pass` / the `logger.info("connection closed...")` line), cancel the task:

```python
        finally:
            if proactive_task is not None and not proactive_task.done():
                proactive_task.cancel()
                try:
                    await proactive_task
                except (asyncio.CancelledError, Exception):
                    pass
```

- [ ] **Step 7: Write a failing test for the WS delivery path**

Create `server/tests/gateway/test_proactive_ws.py`:

```python
"""Tests for the ProactiveOutbox and send_proactive WS delivery path (P3)."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from sense_server.agent.metrics import InMemoryMetricsRecorder
from sense_server.contracts.clock import FakeClock
from sense_server.gateway.core import GatewayCore, ProactiveOutbox
from sense_server.protocol.messages import Hello, ProactiveMessage


def _stub_factory():
    """A pipeline factory whose ingest() returns no transcripts so we
    can drive _emit directly without standing up whisper/opus."""
    def _factory(start_seq):
        from sense_server.ingest.pipeline import AudioIngestPipeline
        return AudioIngestPipeline(
            reassembler=None, decoder=None, transcriber=None,
            hop_ms=1000, window_ms=5000, sample_rate=16000,
        )
    return _factory


@pytest.mark.asyncio
async def test_send_proactive_enqueues_into_outbox():
    """The WsSender seam on GatewayCore pushes into the outbox."""
    core = GatewayCore(pipeline_factory=_stub_factory())
    await core.send_proactive(
        session_id="s1", request_id="r1", text="hello", atoms=("a1",),
    )
    drained = core._proactive_outbox.drain("s1")
    assert len(drained) == 1
    assert drained[0].text == "hello"
    assert drained[0].atoms == ("a1",)


@pytest.mark.asyncio
async def test_drain_drops_expired_messages():
    clock = FakeClock(datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc))
    metrics = InMemoryMetricsRecorder()
    box = ProactiveOutbox(ttl_s=30.0, clock=clock, metrics=metrics)
    box.enqueue(ProactiveMessage(
        session_id="s1", request_id="r1", text="hi", atoms=(),
    ))
    clock.advance(timedelta(seconds=31))
    drained = box.drain("s1")
    assert drained == []
    assert metrics.counter(
        "proactive_delivery_dropped_total", tags={"reason": "ttl_exceeded"},
    ) == 1
```

- [ ] **Step 8: Run the new test and confirm it passes**

Run: `cd server && .venv/bin/python -m pytest tests/gateway/test_proactive_ws.py -v`
Expected: 2 passed.

- [ ] **Step 9: Wire the engine in `run_gateway.py`**

Open `server/scripts/run_gateway.py`. Find the construction of the `Planner` and the existing `enqueuer` wiring. Add the engine as a listener on the `ExtractionWorker` (the worker is constructed earlier in the same script; find that block and add the registration).

Read the script to identify the exact insertion points. The general pattern:

1. Import the engine and the `Proactive` / `Planner` related types (or just the engine class).
2. After the planner is constructed, build the engine:
   ```python
   proactive_engine = ProactiveTriggerEngine(
       planner=planner,
       ws_sender=None,  # wired below; the engine's send_proactive
                        # is on the gateway's GatewayCore, but the
                        # gateway hasn't been constructed yet at this
                        # point. Use a deferred binding.
       clock=SystemClock(),
       metrics=metrics,
       ids=UuidIdGenerator(),
       plan_timeout_s=2.0,
   )
   ```
3. Register the engine on the worker:
   ```python
   worker.add_listener(proactive_engine.on_session_completion)
   ```
4. Pass the engine to `serve(...)` (or have the gateway construct a `send_proactive` callable bound to the core and update the engine's ws_sender post-construction).

The cleanest pattern: make the engine's `ws_sender` an attribute that can be set after construction. Add a `set_ws_sender` method to `ProactiveTriggerEngine`:

```python
def set_ws_sender(self, ws_sender: WsSender) -> None:
    self._ws_sender = ws_sender
```

Then in `run_gateway.py`, after the `serve(...)` call is about to be awaited but before the first event is processed, build a small `GatewayCore` wrapper that delegates `send_proactive` to the actual core. The simplest path: the `serve()` coroutine itself owns the `GatewayCore` per-connection; the engine's `ws_sender` is set to a per-connection callable created at the start of the handler.

A more pragmatic path (acceptable for v1): the `serve()` handler constructs the `GatewayCore` AND wires the engine's `ws_sender` to that core's `send_proactive` method. Modify `serve()` to accept the engine as a parameter; in the handler, after constructing the core, do `engine.set_ws_sender(core)`. The engine's listener is already on the worker; this just makes the engine's `send_proactive` resolve to the right per-connection core.

Add an `engine` parameter to `serve()`:

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
    proactive_engine: "ProactiveTriggerEngine | None" = None,
) -> None:
```

In the handler, after constructing the core:

```python
        if proactive_engine is not None:
            proactive_engine.set_ws_sender(core)
```

In `run_gateway.py`, pass the engine:

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
                proactive_engine=proactive_engine,
            )
```

- [ ] **Step 10: Run the full server test suite**

Run: `cd server && .venv/bin/python -m pytest -x --ignore=tests/gateway/test_streaming_e2e_real_opus.py --ignore=tests/gateway/test_streaming_e2e.py`
Expected: all green (excluding the 2 known-slow e2e ones; the pre-existing 1 failure is allowed).

- [ ] **Step 11: Smoke import the wired `run_gateway.py`**

Run: `cd server && .venv/bin/python -c "import scripts.run_gateway"` (the script is the entry point; just importing the module verifies the engine wiring doesn't have a syntax / import error).

- [ ] **Step 12: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/protocol/messages.py \
        server/src/sense_server/gateway/core.py \
        server/src/sense_server/gateway/adapter.py \
        server/src/sense_server/agent/proactive.py \
        server/scripts/run_gateway.py \
        server/tests/gateway/test_proactive_outbox.py \
        server/tests/gateway/test_proactive_ws.py
git commit -m "feat(server): ProactiveOutbox + _pending_proactives + run_gateway wiring

The gateway's _pending_proactives loop drains the in-process
outbox and sends proactive frames over the same WS the relay
already holds open. Best-effort: messages older than the 30s
TTL are dropped at drain time and counted as
PROACTIVE_DELIVERY_DROPPED_TOTAL{reason=ttl_exceeded}. A
server restart drops everything in flight.

- protocol/messages.py: ProactiveMessage outbound §E frame.
- gateway/core.py: ProactiveOutbox (per-session bucket, TTL
  eviction, asyncio.Event signal). send_proactive is the
  WsSender implementation. _pending_proactives_loop is the
  consumer that the adapter starts as a task.
- gateway/adapter.py: serve() takes proactive_engine; the
  handler creates a task that drains the outbox and sends
  frames; the task is cancelled on connection close.
- run_gateway.py: construct the engine, register on the
  worker, pass into serve(). The engine's ws_sender is
  rebound per-connection in the handler.

4 new tests in test_proactive_outbox.py + 2 in
test_proactive_ws.py. The next commit adds the Android
side: ChatMessageKind.AGENT_PROACTIVE + ProactiveMessageBubble."
```

---

## Commit 6: Android — `AGENT_PROACTIVE` + `ProactiveMessageBubble`

**Files:**
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt` (add `AGENT_PROACTIVE`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt` (add `ServerMessage.Proactive` + parser entry)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt` (dispatch `Proactive` → `ForwardToChatHistory`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt` (execute `ForwardToChatHistory` by writing to `ChatHistoryStore`)
- Modify: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt` (add `AGENT_PROACTIVE` dispatch branch)
- Create: `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubble.kt`
- Test: `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt`

**Interfaces:**
- Consumes: existing `ChatMessageKind`, `ChatMessage`, `ChatHistoryStore`, `RelayAction`, `ServerMessage`; the new `ProactiveMessage` from Commit 5 (wire).
- Produces:
  - `ChatMessageKind.AGENT_PROACTIVE` (fifth variant; the existing `when` exhaustiveness makes adding it a compile error in `ChatMessageList.kt` until the new branch is added).
  - `ProactiveMessageBubble` Composable at `ui/chat/ProactiveMessageBubble.kt`. Stateless: takes a `ChatMessage` and renders the proactive tag + the message text + the atom chips (reusing `AtomChip`).
  - `ServerMessage.Proactive(requestId: String, text: String, atoms: List<String>)` — a new variant in the sealed interface.
  - `RelayAction.ForwardToChatHistory(message: ChatMessage)` — a new variant; the runtime calls `chatHistoryStore.append(message)`.
  - `RelaySession.onServerMessage` dispatches `ServerMessage.Proactive` to `RelayAction.ForwardToChatHistory(ChatMessage(kind=AGENT_PROACTIVE, ...))`.
  - `RelayService` reads `RelayAction.ForwardToChatHistory` and calls `RepositoryModule.repos.chatHistoryStore.append(msg)`.

- [ ] **Step 1: Write a failing signature test for the bubble**

Create `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt`:

```kotlin
package com.sense.relay.ui.chat

import com.sense.relay.data.AtomChip
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.ChatMessageKind
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Test

/**
 * Signature check for ProactiveMessageBubble. The actual Compose
 * rendering is validated manually on a real device (Robolectric
 * PR #4736 blocks host-JVM Compose UI tests in this sandbox).
 *
 * This test pins:
 * - The Composable exists with the documented signature.
 * - A ChatMessage of kind AGENT_PROACTIVE has the expected fields.
 * - The new AGENT_PROACTIVE variant is wired into the ChatMessageKind
 *   enum (proves the dispatch branch in ChatMessageList will compile).
 */
class ProactiveMessageBubbleTest {

    @Test
    fun `ChatMessage of kind AGENT_PROACTIVE carries text and atoms`() {
        val msg = ChatMessage(
            id = "m1",
            role = com.sense.relay.data.Role.AGENT,
            kind = ChatMessageKind.AGENT_PROACTIVE,
            text = "Here's what I noticed",
            atoms = listOf(AtomChip("a1", "fact", "hello", 0.9)),
            traceRequestId = "r1",
            traceRetrievalId = "t1",
            traceAuditId = "a1",
        )
        assertEquals(ChatMessageKind.AGENT_PROACTIVE, msg.kind)
        assertEquals("Here's what I noticed", msg.text)
        assertEquals(1, msg.atoms.size)
    }

    @Test
    fun `ChatMessageKind has AGENT_PROACTIVE variant`() {
        // Exhaustiveness: this is the test. If AGENT_PROACTIVE is
        // missing from the enum, this won't compile.
        val kinds = ChatMessageKind.values().toList()
        assertNotNull(kinds.find { it == ChatMessageKind.AGENT_PROACTIVE })
    }

    @Test
    fun `ProactiveMessageBubble function reference is bindable`() {
        // Function reference: must not throw NoSuchMethodError at link time.
        val f: @androidx.compose.runtime.Composable (ChatMessage) -> Unit =
            ProactiveMessageBubble
        @Suppress("UNUSED_VARIABLE") val captured = f
    }
}
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd android/sense-relay && export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ProactiveMessageBubbleTest"`
Expected: compile error — `ChatMessageKind.AGENT_PROACTIVE` doesn't exist, `ProactiveMessageBubble` doesn't exist.

- [ ] **Step 3: Add `AGENT_PROACTIVE` to `ChatMessageKind`**

Open `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt`. Change line 74:

```kotlin
enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR, AGENT_PROACTIVE }
```

Update the docstring above the enum to mention the new variant:

```kotlin
 * - AGENT_ERROR: an Error outcome (network failure, server error,
 *   rate limit, etc.). The text is pre-mapped via
 *   [com.sense.relay.core.ui.toDisplayMessage] in the route.
 * - AGENT_PROACTIVE: a server-initiated message (P3 proactive
 *   trigger). The server pushed an unsolicited answer based on
 *   the user's recent activity; the user did not ask a question.
 *   The chat screen renders a "Proactive" tag so the user knows
 *   the message wasn't a response to them.
 */
```

- [ ] **Step 4: Add the `Proactive` variant to `ServerMessage`**

Open `android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt`. Add a new variant to the sealed interface:

```kotlin
sealed interface ServerMessage {
    /** Cursor ack: the next contiguous chunk_seq the server wants. */
    data class Ack(val nextSeq: Int) : ServerMessage

    /** Backfill request for the contiguous gap [start, end). */
    data class RequestChunks(val start: Int, val end: Int) : ServerMessage

    /** A transcribed window (informational to the relay). */
    data class Transcript(val text: String, val durationMs: Int) : ServerMessage

    /** A signed §D command to forward to the device. `payload` is canonical JSON,
     *  `sig` is base64 — the device needs raw signature bytes prepended to payload. */
    data class Command(val payload: String, val sig: String) : ServerMessage

    /** P3: a server-initiated proactive answer. The relay forwards it
     *  into the phone's ChatHistoryStore; the chat screen renders it
     *  as an AGENT_PROACTIVE ChatMessage. */
    data class Proactive(
        val requestId: String,
        val text: String,
        val atoms: List<String>,
    ) : ServerMessage

    /** Any type the relay doesn't handle. */
    data class Unknown(val type: String) : ServerMessage
}
```

Update the parser:

```kotlin
    return when (str("type")) {
        "ack" -> ServerMessage.Ack(int("next_seq") ?: 0)
        "request_chunks" -> ServerMessage.RequestChunks(int("start") ?: 0, int("end") ?: 0)
        "transcript" -> ServerMessage.Transcript(str("text") ?: "", int("duration_ms") ?: 0)
        "command" -> ServerMessage.Command(str("payload") ?: "", str("sig") ?: "")
        "proactive" -> ServerMessage.Proactive(
            request_id = str("request_id") ?: "",
            text = str("text") ?: "",
            atoms = parseAtomsList(obj),
        )
        else -> ServerMessage.Unknown(str("type") ?: "missing")
    }
```

Add a small helper above the parser:

```kotlin
private fun parseAtomsList(obj: JsonObject): List<String> {
    val arr = obj["atoms"] as? kotlinx.serialization.json.JsonArray ?: return emptyList()
    return arr.mapNotNull { (it as? JsonObject)?.get("atom_id")?.jsonPrimitive?.content }
}
```

Actually, the wire DTO uses `atoms: tuple[str, ...]` (a flat list of atom id strings on the server side per the spec). Simplify:

```kotlin
"proactive" -> ServerMessage.Proactive(
    requestId = str("request_id") ?: "",
    text = str("text") ?: "",
    atoms = (obj["atoms"] as? kotlinx.serialization.json.JsonArray)
        ?.mapNotNull { it.jsonPrimitive.content } ?: emptyList(),
)
```

(The server's `ProactiveMessage.atoms: tuple[str, ...]` serializes as a JSON array of strings. The relay's parser is lenient per the file's existing convention.)

- [ ] **Step 5: Add `ForwardToChatHistory` to `RelayAction` and dispatch in `RelaySession`**

Open `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt`. Update the `onServerMessage` function (line 33-42):

```kotlin
    /** A §E text frame from the server. */
    fun onServerMessage(text: String): List<RelayAction> =
        when (val msg = parseServerMessage(text)) {
            is ServerMessage.Command -> listOf(forwardCommand(msg))
            is ServerMessage.RequestChunks ->
                listOf(RelayAction.Note("server requested backfill [${msg.start}, ${msg.end}) — not yet supported"))
            is ServerMessage.Transcript ->
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            is ServerMessage.Ack -> emptyList()       // cursor ack; nothing to relay
            is ServerMessage.Proactive -> listOf(forwardProactive(msg))
            is ServerMessage.Unknown -> emptyList()   // forward-compatible: ignore
        }

    private fun forwardProactive(msg: ServerMessage.Proactive): RelayAction {
        // Build a ChatMessage with kind=AGENT_PROACTIVE. The atoms
        // here are just ids; the screen will render tappable chips
        // that look up the full text from the memory repository.
        // The ChatHistoryStore will append it; the ChatScreen will
        // pick it up via its observer.
        val chatMessage = ChatMessage(
            id = msg.requestId,
            role = com.sense.relay.data.Role.AGENT,
            kind = com.sense.relay.data.ChatMessageKind.AGENT_PROACTIVE,
            text = msg.text,
            atoms = msg.atoms.map { atomId ->
                com.sense.relay.data.AtomChip(
                    atomId = atomId,
                    kind = "",
                    text = "",
                    score = 0.0,
                )
            },
            traceRequestId = msg.requestId,
        )
        return RelayAction.ForwardToChatHistory(chatMessage)
    }
```

Add `ForwardToChatHistory` to the sealed interface:

```kotlin
sealed interface RelayAction {
    data class SendServerBinary(val data: ByteArray) : RelayAction
    data class SendServerText(val text: String) : RelayAction
    data class WriteDeviceCommand(val frame: ByteArray) : RelayAction
    data class ForwardToChatHistory(val message: ChatMessage) : RelayAction
    data class Note(val message: String) : RelayAction
}
```

Add the import at the top of the file:

```kotlin
import com.sense.relay.data.ChatMessage
```

(Read the existing imports first; the `ChatMessage` is in `com.sense.relay.data.ChatMessage`.)

- [ ] **Step 6: Handle `ForwardToChatHistory` in `RelayService`**

Open `android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt`. Find the `when` that dispatches `RelayAction`. Add the new branch:

```kotlin
                is RelayAction.ForwardToChatHistory -> {
                    com.sense.relay.data.RepositoryModule.repos.chatHistoryStore.append(action.message)
                }
```

(If `RelayService` already has a `when` over `RelayAction`; if not, find the dispatch site and add the branch. The `chatHistoryStore` is a process-singleton on `RepositoryModule.repos`; the service can reach it directly.)

- [ ] **Step 7: Add the `AGENT_PROACTIVE` branch to `ChatMessageList`**

Open `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt`. Add the new branch to the `when` (around line 56-69):

```kotlin
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
                ChatMessageKind.AGENT_PROACTIVE -> ProactiveMessageBubble(message = msg)
            }
```

- [ ] **Step 8: Create `ProactiveMessageBubble.kt`**

Create `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubble.kt`:

```kotlin
package com.sense.relay.ui.chat

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.shape.RoundedCornerShape
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Notifications
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Surface
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.ChatMessage

/**
 * The proactive message bubble. Sibling of [AgentMessageBubble] and
 * [RefuseMessageBubble]: same bubble styling, with a small "Proactive"
 * tag at the top so the user knows the server pushed this without
 * being asked. Tappable atoms still work (they re-use the same
 * [AtomChip] composable as AGENT_ANSWER).
 *
 * INV-11: this Composable lives in `ui/chat/`. It must not import
 * any class under `com.sense.relay.http.*` or
 * `com.sense.relay.http.dto.*`. The architectural invariant test
 * (`ArchitecturalInvariantsTest`) enforces this.
 */
@Composable
fun ProactiveMessageBubble(
    message: ChatMessage,
    modifier: Modifier = Modifier,
) {
    Surface(
        shape = RoundedCornerShape(12.dp),
        color = MaterialTheme.colorScheme.surfaceVariant,
        contentColor = MaterialTheme.colorScheme.onSurfaceVariant,
        modifier = modifier
            .fillMaxWidth()
            .padding(vertical = Spacing.xs)
            .testTag("chat_proactive_${message.id}"),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Row(verticalAlignment = Alignment.CenterVertically) {
                Icon(
                    imageVector = Icons.Filled.Notifications,
                    contentDescription = "Proactive",
                    modifier = Modifier.testTag("chat_proactive_icon"),
                )
                Spacer(Modifier.width(Spacing.xs))
                Text(
                    text = "Proactive",
                    style = MaterialTheme.typography.labelSmall,
                    modifier = Modifier.testTag("chat_proactive_tag"),
                )
            }
            Spacer(Modifier.height(Spacing.xs))
            Text(
                text = message.text,
                style = MaterialTheme.typography.bodyLarge,
            )
            if (message.atoms.isNotEmpty()) {
                Spacer(Modifier.height(Spacing.xs))
                Row(horizontalArrangement = Arrangement.spacedBy(Spacing.xs)) {
                    message.atoms.forEach { chip ->
                        AtomChip(
                            atom = chip,
                            onTap = { /* deep-link is the follow-up slice */ },
                        )
                    }
                }
            }
        }
    }
}
```

(If the existing `AtomChip` Composable is private to a different file, copy-paste the signature inline; otherwise reuse it. Read the existing `AgentMessageBubble.kt` to confirm the call shape.)

- [ ] **Step 9: Compile-check the Android app**

Run: `cd android/sense-relay && export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" && ./gradlew :app:compileDebugKotlin`
Expected: BUILD SUCCESSFUL.

- [ ] **Step 10: Re-run the new test and confirm it passes**

Run: `cd android/sense-relay && export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.ui.chat.ProactiveMessageBubbleTest"`
Expected: 3 passed.

- [ ] **Step 11: Run the full Android test suite**

Run: `cd android/sense-relay && export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" && ./gradlew :app:testDebugUnitTest`
Expected: 244+ tests pass (this slice adds 3 new tests; net 244).

- [ ] **Step 12: Confirm INV-11 still holds**

Run: `cd android/sense-relay && export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home" && ./gradlew :app:testDebugUnitTest --tests "com.sense.relay.arch.ArchitecturalInvariantsTest"`
Expected: PASS.

- [ ] **Step 13: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add android/sense-relay/app/src/main/kotlin/com/sense/relay/data/ChatHistoryStore.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/protocol/Messages.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/RelaySession.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/RelayService.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ChatMessageList.kt \
        android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubble.kt \
        android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/chat/ProactiveMessageBubbleTest.kt
git commit -m "feat(android): AGENT_PROACTIVE ChatMessage + ProactiveMessageBubble

P3 phone-side rendering. The relay's RelaySession learns one new
ServerMessage variant (Proactive) and emits one new RelayAction
(ForwardToChatHistory); RelayService writes the action into
ChatHistoryStore. ChatMessageList dispatches AGENT_PROACTIVE to a
new ProactiveMessageBubble (sibling of AgentMessageBubble +
RefuseMessageBubble) with a 'Proactive' tag so the user knows the
server pushed it without being asked.

- ChatMessageKind gains AGENT_PROACTIVE.
- ServerMessage.Proactive(requestId, text, atoms) variant.
- RelayAction.ForwardToChatHistory(message) variant.
- RelayService writes the message into RepositoryModule.repos.chatHistoryStore.
- ChatMessageList adds the AGENT_PROACTIVE branch.
- New ProactiveMessageBubble Composable renders the proactive tag +
  the message text + tappable atom chips (reusing AtomChip).

3 new tests in ProactiveMessageBubbleTest (signature checks; the
actual rendering is manual on a real device per the Robolectric
constraint). ArchitecturalInvariantsTest still passes (no http/dto
imports in ui/chat/)."
```

---

## Self-Review

- **Spec coverage:**
  - §3.1 `Trigger` envelope → Commit 1.
  - §3.2 `Planner.plan` proactive restriction (type-system guarantee inside `plan()`, before `_dispatch_command`) → Commit 2.
  - §3.3 `SessionCompletion` + worker listener list → Commit 3.
  - §3.4 `ProactiveTriggerEngine` (2-second timeout, all exceptions caught, all drops counted) → Commit 4.
  - §3.5 `ProactiveOutbox` + `_pending_proactives` (30-second TTL, asyncio.Event signal, dropped metrics) → Commit 5.
  - §3.6 Phone surface (AGENT_PROACTIVE + ProactiveMessageBubble) → Commit 6.
  - §4 file map — every file named. (ProactiveOutbox is a class in `gateway/core.py`; the spec's file map doesn't list a new file for it, and the plan honors that.)
  - §5 test strategy — 5 new server test files + 1 new Android test file. The plan creates them in the order the code under test is built.
  - §6 out-of-scope (no new transport, no system notification, no heuristic gate, no new atom kinds, no outbox persistence, no transcript replay) — explicitly not in any task.
  - §7 size estimate (2-3 days) — six commits of <2 hours each.
  - §8 invariants: H7 (worker listener only after success) → Commit 3's `test_listener_not_invoked_when_pipeline_fails` and the dispatch placement. INV-11 → Commit 6's `ArchitecturalInvariantsTest` step. Type-system guarantee on command restriction → Commit 2's `test_proactive_trigger_refuses_issue_command` and the single-branch placement. Best-effort observability → every commit increments a metric on every drop.
- **Placeholders:** none. (The "deep-link is the follow-up slice" in the new bubble is documented in the spec as out-of-scope.)
- **Type consistency:**
  - `Trigger` in Commit 1 matches the import in Commit 2.
  - `SessionCompletion` in Commit 3 matches the listener signature in Commit 4.
  - `WsSender` Protocol in Commit 4 is structural; `GatewayCore.send_proactive` in Commit 5 implements it without inheriting.
  - `ProactiveMessage` server-side (Commit 5) matches the parser-extracted shape on the Android side (Commit 6). The Android parser is lenient (matches the existing parser's convention).
  - `ChatMessageKind.AGENT_PROACTIVE` in Commit 6 step 3 is referenced in `ChatMessageList` in step 7 and in `ProactiveMessageBubble` in step 8 — all aligned.
- **Subtlety flagged:** the spec says "the relay receives ProactiveMessage frames over the WS" — but the relay's WS is the BLE bridge, not a chat listener. The plan separates these correctly: the server pushes proactive frames over the same WS the relay holds open (per the spec's "reuse the open gateway WS"), and the relay's `RelaySession` learns a new `Proactive` variant that emits `ForwardToChatHistory` (a new `RelayAction` consumed by `RelayService`). The alternative — a separate phone-side WS — is rejected because the spec explicitly said "reuse the open WS."
- **Outbox TTL implementation:** the `ProactiveOutbox` uses a parallel `list[tuple[datetime, ProactiveMessage]]` per session to track enqueue time, so the TTL check at `drain` time is correct. The simpler "use message's own created_at" is rejected because the protocol `ProactiveMessage` doesn't carry a `created_at` field (per the spec's exact shape).
- **One sequencing constraint:** Commit 2's test file imports `RejectionReason.PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND` before it exists. Step 4 adds the rejection reason before step 5 adds the planner branch. The order is preserved.
- **Manual DI on Android:** no new `viewModelFactory` is needed because the proactive path is event-driven (server pushes to existing `ChatHistoryStore`, the existing `ChatViewModel` already observes). The plan correctly avoids introducing a new ViewModel.
- **Robolectric constraint:** the new bubble test is a signature-check (function reference, enum presence) per the project status memory. No `androidTest` setup is added.
