# Sense — SessionIndex cold-start + LLM command prompt (the missing pieces)

> **Goal:** close the two remaining gaps that the 2026-07-19 "wire memory + commands" slice surfaced in production: (1) `SessionIndex` is a pure in-memory dict and never rebuilds from the durable `EventStore`, so a gateway restart makes every "older" session 404, and (2) the agent's system prompt never mentions `issue_command` or the 5 valid command types, so the LLM never emits them — the command path is wired but unreachable.

## 1. Context

The 2026-07-19 "wire memory + commands" slice (committed across 6 feature commits + 3 fix commits) closed the three wiring gaps the user originally reported. A second-pass review of the live system turned up **two more** problems that match the same symptoms in production:

1. **Recordings 404 on older sessions.** The `SessionIndex` is built up only by `GatewayCore._emit` calling `record()` after a live event append. On a server restart, the in-memory dict is empty; the durable `SqliteEventStore` still has every event, but no summary exists in the index. `GET /sessions/{id}` returns 404 because `idx.summary(session_id) is None` (see `http/routes/sessions.py:137-149`). The `run_gateway.py` comment at line 103-105 falsely claims "a restart rebuilds the index from the durable store on demand" — no such code exists. This is a **cold-start bug**, not a write-path bug; the write path is correct.

2. **Commands not recognized.** The system prompt in `agent/context.py:21-47` (`_V1_SYSTEM_PROMPT`) tells the LLM it can return `kind="answer"` or `kind="no_memory"` and stops there. The word "command" and the word "issue_command" appear nowhere in the prompt. Even though `Planner._dispatch_command` is fully implemented and `IssueCommandPayload` accepts 5 command types (`capture_photo`, `record_video`, `start_audio`, `stop_audio`, `request_buffer`), the LLM has no idea the option exists. Asking "record a 3 second video" gets `kind="no_memory"` because the prompt's only positive path is `answer`.

Both bugs are server-side only. The Android side is unaffected — `MemoryScreen`, `MemoryRoute`, `MemoryViewModel`, `MemoryRepository`, and `ChatMessageList` are all wired (committed earlier in the P1/P2 slice).

The M4.3 thread-safety + reconcile-on-start changes (uncommitted in the working tree as of 2026-07-19) close a **third** gap that was preventing the symptom "memories not forming" from being observed in production: the live extraction path was dying silently on cross-thread `sqlite3` access because `SqliteEventStore`/`SqliteAtomStore`/`SqliteMemoryIndex` did not have `check_same_thread=False` + a serialising lock. That fix is staged, all 605 server tests pass with it, and committing it is **Task 1** of the plan below. It is not a new design decision — it is the existing decision, validated, and ready to ship.

## 2. Architecture — two narrow additions + one commit

No new modules, no new dependencies, no wire-protocol changes. The fix is exclusively: a startup-side rebuild pass on `SessionIndex` and a system-prompt version bump that documents the command option to the LLM.

### 2.1 Server — `SessionIndex.rebuild_from_store(event_store)` + call it on startup

```
run_gateway.py start-up sequence (after commit):

  session_index = SessionIndex()
  session_index.rebuild_from_store(store)   # NEW
  # ... rest of wiring unchanged
```

`rebuild_from_store` is a single batched read: it calls `event_store.sessions()` (already added in M4.3), then for each session id calls `event_store.events(session_id)` and replays each event through the same `record()` path that `GatewayCore._emit` uses. This is idempotent — replaying the same events into a fresh `SessionIndex` produces the same summaries as the live path produced originally.

**H7 cross-check:** `rebuild_from_store` does NOT touch the atom store or the memory index. The extraction worker has its own reconcile-on-start pass (M4.3) that advances the per-session cursor from `EventStore.sessions()`. The two passes are independent and can run in either order; one rebuilds the HTTP summary index, the other rebuilds the atom table.

**Performance budget:** A 24h session with 60 minutes of audio at the production window size produces ~12 transcripts per session (5s windows, 300s hop). The summary path reads N events per session and folds them into one dict; reading 12 events is ~2ms. A 100-session history rebuild is ~200ms. Acceptable for a one-shot startup pass; documented in the spec so a future contributor doesn't move it to a worker thread "for performance."

**Thread-safety:** The `SessionIndex` lock (already in place) protects the rebuild. If a live WebSocket connection starts appending events while the rebuild is running, the live `record()` interleaves with the rebuild's `record()` calls under the same lock; the result is a union of both sets, which is the desired outcome.

**Idempotency:** A double-call (e.g. the operator restarts twice in a row, or the test harness calls it twice) is a no-op: the existing summaries are overwritten with the same values.

**Edge case — empty store:** `rebuild_from_store(empty_store)` is a no-op. `total_sessions() == 0` after; `list(limit=20)` returns an empty page. This is the correct cold-start state.

### 2.2 Server — bump `_V1_SYSTEM_PROMPT` to v2 with the command option

```
v1 (current):
  "You may return kind='answer' or kind='no_memory'."

v2 (proposed):
  "You may return kind='answer', kind='no_memory', or kind='issue_command'.
   Use issue_command when the user asks the wearable to do something
   that requires a device action (take a photo, record a video,
   start/stop audio capture, or retrieve a buffer of recent audio).
   The 5 valid command types are:
     capture_photo    (no params)
     record_video     (duration_s: number in [1, 30])
     start_audio      (no params)
     stop_audio       (no params)
     request_buffer   (seconds: number in [1, 60])
   ..."
```

The system prompt becomes v2. The `ContextBuilder.system_prompt_version` literal becomes `"v2"`. The existing test `test_context_builder_marks_prompt_versions` is updated to expect `"v2"`. Audit logs and replays can cite the exact prompt shape that produced any action.

**Prompt-design discipline:**
- Keep the v1 wording for `answer` and `no_memory` (no regression risk).
- Insert the `issue_command` section between the `# Behavior` block and the `# Output format` block. The LLM reads top-to-bottom; the command option should be in the same section as the answer option.
- Mirror the per-type param schema from `agent/validator_command.py:_TYPE_SCHEMAS` so the LLM produces payloads the validator will accept. If the prompt and the validator disagree, the validator wins and the LLM wastes tokens guessing.
- Document the idempotency_key requirement: the LLM must include `idempotency_key: <short string derived from the user request>` so a re-prompt doesn't issue the same command twice.
- Document the `confidence` requirement: commands below the `confidence_autonomous` threshold are refused by the guardrails; the LLM should report a confidence it believes in.

**Test surface (TDD):** the failing test asserts the v2 system prompt contains the substring `issue_command` AND every one of the 5 command types. One test, no mocking.

**No new prompt-versioning layer.** The version string is a string literal on `ContextBuilder`. A future v3 would be a `_V3_SYSTEM_PROMPT` constant and a class-attribute bump. We are not over-engineering this.

## 3. Data flow — `GET /sessions/{id}` after the fix

```
1. Operator restarts `python scripts/run_gateway.py` (preserves events.db, atoms.db, memory_index.db)
2. run_gateway.py:106  → session_index = SessionIndex()
3. run_gateway.py:NEW  → session_index.rebuild_from_store(store)
     for sid in store.sessions():               # SELECT DISTINCT session_id
         for ev in store.events(sid):            # SELECT * WHERE session_id = ?
             session_index.record(ev)            # folds into the in-memory dict
4. HTTP GET /sessions/{id}
     idx.summary(id) is not None → 200 with the summary from the rebuild
     (previously 404, because the in-memory dict was empty)
```

**Side note (out of scope for this slice):** the live extraction worker also has a reconcile-on-start pass (M4.3, Task 1) that runs `ExtractionWorker.reconcile()` inside `await asyncio.to_thread(self._safe_reconcile)` on `worker.start()`. The two passes are decoupled by design — atom extraction is slow (LLM-bound), summary rebuild is fast (SQLite reads). They can run sequentially in any order without blocking each other.

## 4. Error handling

- **`rebuild_from_store` fails on a single session:** swallow + log; the rebuild continues with the next session. The live path will re-populate the missing summary on the next event append. Document the failure in the commit message so a future operator knows the durable store and the summary index may diverge briefly.
- **`rebuild_from_store` finds zero events:** no-op. The index stays empty; `/sessions` returns an empty page.
- **The store is None (test path):** `rebuild_from_store(None)` raises `TypeError`; tests that don't pass a store skip the call. The run_gateway.py call site is unconditional because the live script always has a `store`.
- **System-prompt v2 test fails because the LLM still emits `no_memory` instead of `issue_command`:** the test is a deterministic content check, not an LLM behavior check. The unit test asserts the prompt STRING contains the right substrings; live LLM behavior is verified by a manual smoke (`curl -X POST /agent -d '{"text":"record a 3 second video"}'` should return `outcome: "issue_command"`).

## 5. Testing

### Server

- **New test (rebuild):** `tests/sessions/test_index.py::test_rebuild_from_store_populates_summaries_from_event_store` — pre-seed `SqliteEventStore` with events for 2 sessions, call `SessionIndex.rebuild_from_store(store)`, assert `summary("s1")` and `summary("s2")` have the correct `event_count` + `transcript_count` + `preview`. Mirror test for `InMemoryEventStore`.
- **New test (rebuild idempotent):** `test_rebuild_from_store_is_idempotent` — call it twice, assert no duplicate sessions and identical summaries.
- **New test (rebuild empty):** `test_rebuild_from_store_empty_store_is_noop` — `rebuild_from_store(InMemoryEventStore())` leaves `total_sessions() == 0`.
- **New test (rebuild failure isolation):** `test_rebuild_from_store_skips_failing_session` — if a session's events cause `record()` to raise (e.g. by passing a malformed event), the rebuild continues with the next session. Defensive: a single bad event in the durable store should not block the whole rebuild.
- **Existing test (prompt version):** `tests/agent/test_context.py::test_context_builder_marks_prompt_versions` is updated to assert `"v2"` instead of `"v1"`. The test is one line; the change is intentional.
- **New test (prompt content):** `tests/agent/test_context.py::test_context_builder_v2_prompt_documents_command_option` — assert `prompt.system` contains the substring `issue_command` AND all 5 command types AND the words `idempotency_key` and `confidence`. One test, no mocking.
- **Existing tests stay green:** all 605 server tests must remain green at every commit.

### Android

No Android changes in this slice. The existing 244 unit tests must remain green.

### INV-11

No Android UI changes; invariant test stays green.

## 6. Out of scope (explicit)

- **A real `MemoryScreen` rebuild hook for resumable search sessions** — Android's `MemoryViewModel` is per-screen; a future slice could persist the search query across app restarts. Out of scope.
- **The proactive trigger's `transcript` field** — v1 sends empty string; a future slice should capture the actual transcript so the proactive prompt has context. Out of scope.
- **A second `MemoryScreen` state for "loading rebuild"** — the rebuild is fast enough to complete before the gateway serves its first HTTP request. Out of scope.
- **`relay_connected=False` → `True` at runtime** — the BLE status characteristic is the P3 follow-up. Out of scope.
- **The proactive ISSUE_COMMAND prohibition in `Planner.plan`** — already committed (P3 slice). Out of scope.

## 7. Design principles reaffirmed

- **Server is intelligence; wearable is deterministic sensor/actuator.** No change.
- **One decision pipeline** — unchanged.
- **Contracts first** — no new contracts.
- **Single responsibility per component** — preserved: `SessionIndex.record` stays the only mutator; `rebuild_from_store` is a thin wrapper over `record`.
- **No rewrites of working code** — only one new method, one new test, one prompt-version bump.
- **TDD** — every fix has a failing test written first.

## 8. Files touched (anticipated)

Server:
- `server/src/sense_server/sessions/index.py` — add `rebuild_from_store(event_store)` method.
- `server/src/sense_server/agent/context.py` — bump `_V1_SYSTEM_PROMPT` to v2; bump `system_prompt_version = "v2"`; document `issue_command` and the 5 command types in the prompt text.
- `server/scripts/run_gateway.py` — call `session_index.rebuild_from_store(store)` after `session_index = SessionIndex()`.
- `server/tests/sessions/test_index.py` — add 4 rebuild tests.
- `server/tests/agent/test_context.py` — update 1 test (prompt version); add 1 test (prompt content).

Uncommitted (M4.3) — commit only, no new code:
- `server/src/sense_server/events/store.py`
- `server/src/sense_server/memory/extraction_worker.py`
- `server/src/sense_server/memory/index.py`
- `server/src/sense_server/memory/store.py`
- `server/tests/events/test_store.py`
- `server/tests/memory/test_atom_store.py`
- `server/tests/memory/test_extraction_worker.py`
- `server/tests/memory/test_extraction_worker_listeners.py`
- `server/tests/memory/test_index.py`

## 9. Verification gate

After all tasks land:
- `cd server && .venv/bin/python -m pytest -x` — all 605+ tests pass.
- `cd android/sense-relay && ./gradlew testDebugUnitTest` — all 244 tests pass.
- Manual: start the gateway with `SENSE_LLM_MODEL` + `SENSE_EMBED_MODEL` set; `curl /sessions` returns the historical sessions (rebuild populated the index); `curl -X POST /agent -d '{"text":"record a 3 second video"}'` returns `outcome: "issue_command"` with a `command_id` (LLM saw the v2 prompt and emitted the new kind).
