# Sense — wire memory + commands (the P1/P2 wiring gaps)

> **Goal:** close three independent wiring gaps so that the live `run_gateway.py` plus the Android app actually (1) extract memory from captured transcripts, (2) issue commands when the agent emits `issue_command`, and (3) let the user browse atoms in a real `MemoryScreen`.

## 1. Context

The server's P1 (memory brain) and P2 (agent: answers + commands) work end-to-end **in unit and integration tests** — the planner, validator, guardrails, command dispatcher, extraction worker, atom store, memory index, and `OpenAICompatibleAgentLLM` are all implemented, all green on 577+ tests, and `tests/integration/test_command_issue.py` proves the command path issues signed commands.

The live entry point `server/scripts/run_gateway.py` is missing three seams that the tests build by hand:

1. The gateway hot path never enqueues extracted events to the worker, so the atom table is empty and `/agent` always short-circuits to `Refuse(NO_SUPPORTING_MEMORY)`.
2. The `Planner(...)` call in `run_gateway.py` doesn't pass `command_validator`, `command_guardrails`, or `dispatcher`, so the planner's command-path guard (`agent/planner.py:241`) refuses every command with "command dispatch is not configured on this server."
3. The Android `RepositoryModule` doesn't construct a `MemoryRepository`; the `MemoryRoute` is a stub that renders a hard-coded "coming soon" screen instead of a real list of atoms.

Each bug is a different fix site; they are sequenced because the chat "memory non existent" message only resolves once #1 is in, and Android's Memory screen only has anything to show once #1 has produced atoms to fetch.

## 2. Architecture — no new components, only wiring

No new modules, no new dependencies, no new tests files for the server (one new wiring test). The fix is exclusively the missing seams.

### 2.1 Server — gateway enqueuer wiring (fix 1)

```
incoming audio  ──►  GatewayCore.on_audio  ──►  AudioIngestPipeline.ingest
                                                  │
                                                  ▼
                                          GatewayCore._emit(transcripts)
                                                  │
                                                  ├── self._store.append(event)         (existing)
                                                  ├── self._session_index.record(event) (existing)
                                                  └── self._enqueuer.enqueue(session_id) ◄── NEW
                                                                                            │
                                                                                            ▼
                                                          ExtractionWorker._run loop drains
                                                          (already running, started in run_gateway)
```

`GatewayCore.__init__` gains one parameter, `enqueuer: ExtractionEnqueuer | None = None`. `serve()` in `gateway/adapter.py` gains the same parameter and threads it into each per-connection `GatewayCore`. `run_gateway.py` passes the existing `enqueuer` (the one already attached to the worker) to `serve(...)`.

**Failsafe:** the enqueuer has a bounded queue (`asyncio.Queue`, capacity 1024) with `enqueue` overflow-incrementing `EXTRACTION_QUEUE_OVERFLOW_TOTAL`. Overflow is observable via `/metrics`; a hot-path audio packet is never blocked.

**Idempotency:** `ExtractionEnqueuer.enqueue` dedupes the same session id while pending. Per-event enqueue is therefore safe and cheap; the worker is still the single point that advances the cursor.

### 2.2 Server — planner command-path wiring (fix 2)

```python
# run_gateway.py:160-181 — current
planner = Planner(
    retriever=..., context_builder=..., llm=..., validator=..., guardrails=...,
    audit=..., metrics=..., capability_provider=..., clock=..., ids=...,
    # ← command_validator, command_guardrails, dispatcher all None
)

# After
planner = Planner(
    ...same args...,
    command_validator=CommandValidator(),
    command_guardrails=StrictCommandGuardrails(
        confidence_autonomous=agent_config.guardrails.confidence_autonomous,
    ),
    dispatcher=dispatcher,  # already constructed earlier
)
```

The Planner's existing dispatch path (`_dispatch_command` in `agent/planner.py:205-348`) is already correct; it just needs the dependencies passed in. The integration test `test_command_issue.py:186-198` already proves the path works once these three deps are provided.

**Resource note (not in this slice):** `StrictCommandGuardrails` reads `DeviceResourceStatus.relay_connected`. The default `DeviceResourceStatus()` has `relay_connected=False`, which would refuse every command with "device is not connected to the relay." This slice accepts the failure mode for the case where a command is issued without a real device: the `relay_connected` flag is set by a future BLE status characteristic (P3 work). For this slice, we document the behavior in the spec and the user can test commands by either (a) flipping the constant in `ConstantCapabilityProvider` for a one-off, or (b) waiting for P3. The wiring is the same either way.

### 2.3 Android — MemoryRepository + real MemoryScreen (fix 3)

```
RepositoryModule.init
  └── clientProvider (existing — config-aware SenseHttpClient)
        ├── CommandApi (existing)
        ├── AgentApi (existing)
        └── MemoryApi (new)  ──►  MemoryRepository (new field on Repositories)
                                       │
                                       ▼
                          MemoryViewModel(repo)  (exists; only instantiated in tests)
                                       │
                                       ▼
                          MemoryRoute (real, new) — was a stub
                                       │
                                       ▼
                          MemoryScreen (real, new) — was a stub
                                       ├── search TextField + button
                                       ├── list of atoms (title + snippet + timestamp)
                                       ├── tap → AtomDetail route
                                       └── empty / loading / error states
```

The `MemoryApi` and `MemoryRepository` already exist with unit tests green; only the production wiring + UI Composable are missing. Pattern matches `commandRepository` (`RepositoryModule.kt:94-99`) and `agentRepository` (`:104-109`) exactly.

## 3. Data flow — `POST /agent` end-to-end after the fix

```
1. Phone BLE-relays audio → gateway WebSocket
2. GatewayCore._emit stores event, enqueues session to worker
3. ExtractionWorker.process_session (in background thread)
     → pipeline.run: extract (LLM) → version-stamp → embed → index → atom_store.save
     → atom_store.set_cursor(session, last_seq)
4. User types chat question → ChatScreen.ask → AgentRepository → POST /agent
5. Planner.plan: retriever.search finds atoms → LLM reasons → returns Answer with atom_ids
6. ChatMessageList renders Answer bubble with AtomChip row
7. (If the LLM emits issue_command instead) Planner._dispatch_command
     → CommandValidator.check (allowlist + param bounds)
     → StrictCommandGuardrails.check (capability + resource + confidence)
     → CommandDispatcher.issue → SqliteCommandStore.save → signed pending
8. (Existing) Pending commands are pulled by the device on next WS connection
```

## 4. Error handling

- **Hot path enqueue failure (overflow / closed loop):** the `ExtractionEnqueuer.enqueue` never raises. Overflow increments `EXTRACTION_QUEUE_OVERFLOW_TOTAL` and drops the duplicate. Reconciliation (`ExtractionWorker.reconcile`) catches up on restart.
- **Gateway enqueuer is None (back-compat with tests):** `serve()` and `GatewayCore.__init__` default `enqueuer=None`. The hot path checks `if self._enqueuer is not None: self._enqueuer.enqueue(...)` — existing tests that don't pass an enqueuer continue to work.
- **Android `MemoryRepository` not provisioned:** the `clientProvider` throws `IOException("not provisioned")`; `MemoryRepository.search` catches it (after wrapping) and returns `MemoryOutcome.Error(ErrorCode.INTERNAL_ERROR, "not provisioned")`; the UI shows an error state. Same pattern as CommandRepository.
- **Android search with empty query:** existing `MemoryViewModel.search` returns early when `q.isEmpty()`. No change.
- **Server planner command path with `relay_connected=False`:** the guardrail refuses with "device is not connected to the relay." Accepted behavior for this slice; the next P3 slice wires the device status characteristic to flip the flag at runtime.

## 5. Testing

### Server

- **Existing:** 577+ tests stay green. No new test files for the planner command-path fix (it is already covered by `tests/integration/test_command_issue.py`).
- **New:** `tests/gateway/test_extraction_wiring.py` already exists; add `test_gateway_core_enqueues_after_emit` — drive `GatewayCore` with a stub `event_store` and a real `ExtractionEnqueuer` + `ExtractionWorker`, confirm `enqueuer.qsize()` drops to 0 and the atom appears in the store within the worker's drain window.
- **Manual smoke:** start the gateway, replay a session via `scripts/run_device_sim.py`, then `curl -H 'Authorization: Bearer <token>' 'http://localhost:8766/memory?q=test'`. Expect `atoms` to be non-empty after ~10s.

### Android

- **New:** `MemoryRepositoryTest` — assert `MemoryRepository.search` returns `MemoryOutcome.Success` on 200 and `Error` on 4xx/5xx. Mirrors `CommandRepositoryTest`.
- **Existing:** `MemoryViewModelTest` (5 tests) stays green. `ChatViewModelTest` stays green. `CommandsViewModelTest` + `CommandsViewModelPollingTest` stay green. `BottomBarTest` stays green.
- **Manual smoke:** on a provisioned device, open Chat → ask "what did I say about X" → expect an Answer with chips. Open the refuse link → land on a real Memory screen with a search box. Run a search → see atoms.

### INV-11 (UI-package boundary)

The new `MemoryRoute` is the UI seam; the `MemoryViewModel` already lives in `ui/memory/`; the `MemoryRepository` lives in `data/`. No new INV-11 violation. `ArchitecturalInvariantsTest` stays green.

## 6. Out of scope (explicit)

- **Real `MemoryScreen` polish** beyond the minimum: no infinite scroll, no per-session filter chips, no fuzzy search. Plain text input + debounced search + scrollable list + tap-to-AtomDetail. Polish in a follow-up slice.
- **Device status characteristic → `relay_connected` flag** — P3 work. This spec does not touch the gateway's `ConstantCapabilityProvider` default.
- **P3 proactive trigger, P4 firmware executors** — out of scope per the project status doc.
- **`SetupActivity.kt` direct `SenseHttpClient` import** — pre-existing INV-11 allow-list, unchanged.
- **Real-device BLE bring-up** — sandbox-bound; explicitly deferred per project status.

## 7. Design principles reaffirmed

- **Server is intelligence; wearable is deterministic sensor/actuator.** No change — this slice only widens the seams that the existing protocol already declared.
- **One decision pipeline** — unchanged.
- **Contracts first** — no new contracts; only wire existing ones.
- **Single responsibility per component** — preserved (we add one field, not new logic).
- **No rewrites of working code** — only three small wiring edits.
- **TDD** — every fix has a failing test written first.

## 8. Files touched (anticipated)

Server:
- `server/src/sense_server/gateway/core.py` — add `enqueuer` param; enqueue in `_emit`.
- `server/src/sense_server/gateway/adapter.py` — thread `enqueuer` into `serve()` → `GatewayCore`.
- `server/scripts/run_gateway.py` — pass `enqueuer` to `serve(...)`; pass `command_validator`/`command_guardrails`/`dispatcher` to `Planner(...)`.
- `server/tests/gateway/test_extraction_wiring.py` — add `test_gateway_core_enqueues_after_emit`.

Android:
- `android/sense-relay/app/src/main/kotlin/com/sense/relay/data/RepositoryModule.kt` — add `memoryRepository: MemoryRepository` to `Repositories`; construct via `apiProvider` factory.
- `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryRoute.kt` — replace stub with `viewModel(factory)` and collect `state`.
- `android/sense-relay/app/src/main/kotlin/com/sense/relay/ui/memory/MemoryScreen.kt` — replace stub with a real Composable (search bar + list).
- `android/sense-relay/app/src/test/kotlin/com/sense/relay/data/MemoryRepositoryTest.kt` — new (mirrors `CommandRepositoryTest`).
- `android/sense-relay/app/src/test/kotlin/com/sense/relay/ui/memory/MemoryRouteTest.kt` — new (Robolectric UI test for the new Composable).
- `android/sense-relay/app/src/test/kotlin/com/sense/relay/arch/ArchitecturalInvariantsTest.kt` — likely no change; if the new `MemoryRoute` accidentally imports from `data.MemoryApi`, the test will fail and we'll add the missing import alias.

## 9. Verification gate

After all three fixes land:
- `cd server && .venv/bin/python -m pytest -x` — all green.
- `cd android/sense-relay && ./gradlew test` — all green (236+ tests).
- Manual: gateway up + a few minutes of audio + `curl /memory` returns atoms.
- Manual: `curl -X POST /agent -d '{"text":"record a 3 second video"}'` returns `outcome: issue_command` with a `command_id`.
- Manual: open Chat in the app → ask a question → see Answer; tap refuse link → see Memory with atoms; search → see filtered list.
