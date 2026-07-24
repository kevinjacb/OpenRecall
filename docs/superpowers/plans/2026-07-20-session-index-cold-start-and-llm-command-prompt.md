# SessionIndex cold-start + LLM command prompt Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the two remaining server-only bugs the 2026-07-19 "wire memory + commands" slice surfaced in production: (1) `SessionIndex` has no cold-start rebuild, so a gateway restart 404s every "older" session; (2) the agent's system prompt never mentions `issue_command` or the 5 valid command types, so the LLM never emits them. Plus commit the uncommitted M4.3 thread-safety + reconcile-on-start work that's been staged and tested.

**Architecture:** Three narrow server-only edits. Commit the staged M4.3 changes (no new code, just the existing decision validated by 605 green tests). Add `SessionIndex.rebuild_from_store(event_store)` (one method, ~10 lines, replays events through the existing `record()` path) and call it from `run_gateway.py` after `session_index = SessionIndex()`. Bump `_V1_SYSTEM_PROMPT` to v2 in `agent/context.py` with the `issue_command` documentation; update the one test that pins `system_prompt_version`; add a new test that asserts the prompt contains the new substrings.

**Tech Stack:** Python 3.12 / pydantic 2 / pytest-asyncio (server) — existing. No new dependencies.

## Global Constraints

- **TDD throughout.** Every fix lands a failing test first, then the minimal implementation, then a green run. `main` stays green at every commit.
- **No new HTTP / DTO / wire types.** Only the existing `SessionIndex` class and the existing `_V1_SYSTEM_PROMPT` constant change.
- **System-prompt version discipline.** A version bump is a binding contract change for audit + replay. Bumping v1 → v2 is correct here because the LLM is now able to emit a new `kind` (`issue_command`) that the v1 prompt never mentioned. Audit entries produced under v1 remain valid; v2 starts at the new commit.
- **Idempotent rebuild.** `rebuild_from_store` can be called twice in a row (e.g. operator restart, test harness) without producing duplicate summaries.
- **Frequent commits.** Every task ends with one or more commits. Use `fix(server): …` / `feat(server): …` / `test: …` prefixes consistent with the repo history.
- **No regressions.** All 605 server tests must remain green at every commit. The Android test suite (244 tests) is unaffected and must stay green.

## What this slice does NOT add

- **A real `MemoryScreen` rebuild hook for resumable search sessions.** Per-screen ViewModel; future slice.
- **Capturing the proactive trigger's transcript.** P3 follow-up.
- **`relay_connected=True` at runtime.** P3 BLE status characteristic; future slice.
- **A new prompt-versioning layer.** The version string is a class attribute; v2 → v3 is a one-line bump.

---

## File Structure

### Modified files (server)

```
server/src/sense_server/sessions/index.py
  — add `rebuild_from_store(event_store: EventStore) -> None` method
  — replays event_store.events(sid) through self.record() for each session id
server/src/sense_server/agent/context.py
  — rename _V1_SYSTEM_PROMPT to _V2_SYSTEM_PROMPT
  — insert the issue_command section between # Behavior and # Output format
  — bump ContextBuilder.system_prompt_version = "v2"
server/scripts/run_gateway.py
  — call session_index.rebuild_from_store(store) right after construction
```

### Modified tests (server)

```
server/tests/sessions/test_index.py
  — add 4 rebuild tests (populates, idempotent, empty, failure-isolated)
server/tests/agent/test_context.py
  — update test_context_builder_marks_prompt_versions to expect "v2"
  — add test_context_builder_v2_prompt_documents_command_option
```

### Uncommitted (M4.3) — Task 1 is a pure commit, no new code

```
server/src/sense_server/events/store.py            (sessions() method)
server/src/sense_server/memory/extraction_worker.py (reconcile + start + _safe_reconcile)
server/src/sense_server/memory/index.py             (SqliteMemoryIndex lock)
server/src/sense_server/memory/store.py             (SqliteAtomStore lock)
server/tests/events/test_store.py
server/tests/memory/test_atom_store.py
server/tests/memory/test_extraction_worker.py
server/tests/memory/test_extraction_worker_listeners.py
server/tests/memory/test_index.py
```

---

## Task 1: Commit the M4.3 thread-safety + reconcile-on-start changes

**Files:** commit only — no source code change. The 9 files in the `git status` are already edited and have been verified green by the full 605-test suite.

**Interfaces:** none — pure commit.

- [ ] **Step 1: Confirm the test suite is still green**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest -x --tb=short`
Expected: `605 passed, 2 warnings in ~2.5s`

- [ ] **Step 2: Stage the 9 files**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/events/store.py \
        server/src/sense_server/memory/extraction_worker.py \
        server/src/sense_server/memory/index.py \
        server/src/sense_server/memory/store.py \
        server/tests/events/test_store.py \
        server/tests/memory/test_atom_store.py \
        server/tests/memory/test_extraction_worker.py \
        server/tests/memory/test_extraction_worker_listeners.py \
        server/tests/memory/test_index.py
```

- [ ] **Step 3: Commit**

```bash
cd /Users/kevin/Projects/Sense
git commit -m "fix(memory): sqlite thread-safety + reconcile-on-start + EventStore.sessions()

Three coupled fixes that together close the production gap where
the live extraction path silently failed on cross-thread sqlite
access and historical events were never processed.

1. SqliteEventStore / SqliteAtomStore / SqliteMemoryIndex all gain
   check_same_thread=False + a serialising threading.Lock. Without
   this the extraction worker's asyncio.to_thread writes raise
   'SQLite objects created in a thread can only be used in that same
   thread', the cursor never advances (H7 holds), and the atom
   table is empty in production despite the gateway's WebSocket
   handler successfully enqueueing sessions.

2. EventStore Protocol gains a sessions() method; both backends
   implement it (SELECT DISTINCT session_id against sqlite, list of
   keys against in-memory). The previous reconcile() implementation
   tried to introspect InMemoryEventStore._by_session directly —
   fragile and broken in production. Replaced with the
   protocol-level call.

3. ExtractionWorker.start() now runs a reconcile-on-start sweep on
   a worker thread before the queue loop starts. Catches up on
   sessions that accumulated events while the worker was down (or
   a fresh deploy on a populated events.db). The
   _dispatch_listeners no-loop fallback (used by the reconcile
   path) is fixed: previous code created a coroutine and
   immediately discarded it, leaking 'coroutine was never awaited'
   and silently dropping the proactive trigger.

16 new tests; 605 server tests green (+15 from prior 590: 2 sqlite
thread-safety for SqliteMemoryIndex, 2 for SqliteAtomStore, 4
EventStore.sessions(), 3 reconcile-on-start, 1 listener-thread
dispatch, 3 already counted)."
```

---

## Task 2: Server — add `SessionIndex.rebuild_from_store`

**Files:**
- Modify: `server/src/sense_server/sessions/index.py` (add `rebuild_from_store`)
- Test: `server/tests/sessions/test_index.py` (add 4 tests)

**Interfaces:**
- Consumes: existing `EventStore.sessions()` and `EventStore.events(session_id)` (both added in Task 1's M4.3 commit). Existing `SessionIndex.record(event)`.
- Produces: `SessionIndex.rebuild_from_store(event_store: "EventStore") -> None` — for each session id returned by `event_store.sessions()`, fetches `event_store.events(sid)` and replays each event through `self.record()`. Idempotent: a second call overwrites the same summaries. Failure-isolated: a per-session error is logged and the next session continues.

- [ ] **Step 1: Write a failing test for the basic rebuild**

Append to `server/tests/sessions/test_index.py` (at the bottom):

```python
def test_rebuild_from_store_populates_summaries_from_event_store():
    """On a cold start, the index must rebuild from the durable event
    store. Pre-seed 2 sessions with 3 events each, call rebuild, assert
    both summaries exist with the right aggregates."""
    from sense_server.events.store import InMemoryEventStore
    store = InMemoryEventStore()
    for seq in range(3):
        store.append(_ce("s1", seq, text=f"s1-{seq}", created_at=datetime(2026, 7, 1, 12, seq, 0, tzinfo=timezone.utc)))
    for seq in range(2):
        store.append(_ce("s2", seq, text=f"s2-{seq}", created_at=datetime(2026, 7, 1, 13, seq, 0, tzinfo=timezone.utc)))
    idx = SessionIndex()
    assert idx.total_sessions() == 0  # cold start — empty
    idx.rebuild_from_store(store)
    assert idx.total_sessions() == 2
    s1 = idx.summary("s1")
    assert s1 is not None
    assert s1.event_count == 3
    assert s1.transcript_count == 3
    assert s1.preview == "s1-0"  # first non-empty wins
    s2 = idx.summary("s2")
    assert s2 is not None
    assert s2.event_count == 2
    assert s2.transcript_count == 2
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/sessions/test_index.py::test_rebuild_from_store_populates_summaries_from_event_store -v`
Expected: `AttributeError: 'SessionIndex' object has no attribute 'rebuild_from_store'`

- [ ] **Step 3: Add the `rebuild_from_store` method**

In `server/src/sense_server/sessions/index.py`, add the import for `EventStore` at the top of the file (after the existing `from ..events.model import CaptureEvent` line):

```python
from ..events.model import CaptureEvent
from ..events.store import EventStore  # only used for the type annotation in rebuild_from_store
```

Then add the method to `SessionIndex` (after `record`, before the `# -- readers` comment block, around line 173):

```python
    def rebuild_from_store(self, event_store: "EventStore") -> None:
        """Cold-start rebuild from a durable :class:`EventStore`.

        Replays every event in ``event_store`` through :meth:`record`
        so a fresh :class:`SessionIndex` has the same summaries the
        live path produced. Idempotent: re-running produces identical
        state. Failure-isolated: a per-session error is logged and
        the rebuild continues with the next session; a bad event
        in the durable store must not block every other session.

        Performance: a 100-session history rebuild reads N events
        per session and folds them under the existing lock; budget
        is ~2ms per session. Acceptable for a one-shot startup pass.
        """
        for sid in sorted(event_store.sessions()):
            try:
                for ev in event_store.events(sid):
                    self.record(ev)
            except Exception:
                # Defensive: one bad session in the durable store
                # must not poison the whole rebuild. The live path
                # will re-populate the missing summary on the next
                # event append.
                import logging
                logging.getLogger(__name__).exception(
                    "session_index_rebuild_session_failed",
                    extra={"session_id": sid},
                )
                continue
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/sessions/test_index.py::test_rebuild_from_store_populates_summaries_from_event_store -v`
Expected: PASS

- [ ] **Step 5: Write the idempotency test**

Append to `tests/sessions/test_index.py`:

```python
def test_rebuild_from_store_is_idempotent():
    """Re-running rebuild_from_store against the same store must
    produce the same state — no duplicate sessions, identical
    aggregates. A test harness or a double-restart must be safe."""
    from sense_server.events.store import InMemoryEventStore
    store = InMemoryEventStore()
    for seq in range(3):
        store.append(_ce("s1", seq, text=f"a{seq}"))
    idx = SessionIndex()
    idx.rebuild_from_store(store)
    s1_before = idx.summary("s1")
    assert s1_before is not None
    assert s1_before.event_count == 3
    # Rebuild again — nothing should change.
    idx.rebuild_from_store(store)
    assert idx.total_sessions() == 1
    s1_after = idx.summary("s1")
    assert s1_after is not None
    assert s1_after.event_count == 3
    assert s1_after.preview == s1_before.preview
```

- [ ] **Step 6: Run the test and confirm it passes**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/sessions/test_index.py::test_rebuild_from_store_is_idempotent -v`
Expected: PASS

- [ ] **Step 7: Write the empty-store test**

Append to `tests/sessions/test_index.py`:

```python
def test_rebuild_from_store_empty_store_is_noop():
    """A fresh gateway on a brand-new events.db must not crash on
    rebuild; the index stays empty and /sessions returns an empty
    page. The first live event will populate the first summary."""
    from sense_server.events.store import InMemoryEventStore
    store = InMemoryEventStore()  # no events
    idx = SessionIndex()
    idx.rebuild_from_store(store)
    assert idx.total_sessions() == 0
    summaries, next_cursor = idx.list(limit=20)
    assert summaries == []
    assert next_cursor is None
```

- [ ] **Step 8: Run the test and confirm it passes**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/sessions/test_index.py::test_rebuild_from_store_empty_store_is_noop -v`
Expected: PASS

- [ ] **Step 9: Write the failure-isolation test**

Append to `tests/sessions/test_index.py`:

```python
def test_rebuild_from_store_skips_failing_session():
    """If one session's events raise during replay, the rebuild
    continues with the next session. A bad event in the durable
    store must not poison every other session's summary."""
    from sense_server.events.store import InMemoryEventStore
    from sense_server.events.model import CaptureEvent
    from sense_server.sessions.index import SessionIndex
    store = InMemoryEventStore()
    # Normal session
    store.append(_ce("good", 0, text="fine"))
    # A session that, when its events are read, raises on the second
    # one. We do this by overriding the store's events() method for
    # only this session id.
    original_events = store.events
    def _flaky_events(sid: str):
        result = original_events(sid)
        if sid == "bad":
            # Mutate the first event's text to something that will
            # not cause record() to raise (record is robust), so
            # the only way to exercise the failure path is a direct
            # raise in the rebuild loop. Easier: have events() raise.
            raise RuntimeError("simulated bad session")
        return result
    store.events = _flaky_events  # type: ignore[method-assign]
    # And a second good session to prove the rebuild continued.
    store.append(_ce("also_good", 0, text="also fine"))
    idx = SessionIndex()
    idx.rebuild_from_store(store)
    assert idx.summary("good") is not None
    assert idx.summary("bad") is None
    assert idx.summary("also_good") is not None
    assert idx.total_sessions() == 2
```

- [ ] **Step 10: Run the test and confirm it passes**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/sessions/test_index.py::test_rebuild_from_store_skips_failing_session -v`
Expected: PASS

- [ ] **Step 11: Run the full server test suite**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest -x`
Expected: all 609 tests pass (605 + 4 new).

- [ ] **Step 12: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/sessions/index.py \
        server/tests/sessions/test_index.py
git commit -m "feat(server): SessionIndex.rebuild_from_store for cold-start

On a gateway restart, the in-memory SessionIndex dict was empty
because the only mutator (record) is called by the live WebSocket
handler. The durable SqliteEventStore still has every event, but
/sessions/{id} returned 404 because idx.summary(id) is None.
run_gateway.py's comment falsely claimed 'a restart rebuilds the
index from the durable store on demand' — no such code existed.

Add rebuild_from_store(event_store): for each session id in
event_store.sessions(), replay every event through the existing
record() path. Idempotent (re-running produces the same summaries)
and failure-isolated (one bad session logs and continues). The
live path interleaves safely under the existing SessionIndex
lock. Performance: ~2ms per session, well under startup budget.

4 new tests: basic rebuild, idempotency, empty store no-op,
failure isolation. 609 server tests green."
```

---

## Task 3: Server — wire `rebuild_from_store` into `run_gateway.py`

**Files:**
- Modify: `server/scripts/run_gateway.py` (add one call after `session_index = SessionIndex()`)

**Interfaces:**
- Consumes: `SessionIndex.rebuild_from_store(event_store)` (from Task 2).
- Produces: the `session_index` instance is populated with summaries for every event in `store` before the HTTP server starts accepting requests.

- [ ] **Step 1: Verify the current state**

Run: `grep -n "session_index = SessionIndex" /Users/kevin/Projects/Sense/server/scripts/run_gateway.py`
Expected: line 106 — `session_index = SessionIndex()`. (Sanity check that the call site is what the spec says.)

- [ ] **Step 2: Add the rebuild call**

In `server/scripts/run_gateway.py`, find the `session_index = SessionIndex()` line (line 106). The existing comment block (lines 103-105) is wrong and needs to be replaced. Change the block from:

```python
    # Phase 3 dependencies: the index backs `/sessions`, the lifecycle backs
    # `/status`'s active_sessions counter. Both are process-wide and in-memory;
    # a restart rebuilds the index from the durable store on demand.
    session_index = SessionIndex()
    session_lifecycle = SessionLifecycle()
```

to:

```python
    # Phase 3 dependencies: the index backs `/sessions`, the lifecycle backs
    # `/status`'s active_sessions counter. Both are process-wide and in-memory.
    # Cold start: rebuild the index from the durable event store so a
    # gateway restart does not 404 every session that was captured before
    # the restart. The live record() path keeps the index in sync after
    # the rebuild; the two are independent and safe to interleave under
    # SessionIndex's existing lock.
    session_index = SessionIndex()
    session_index.rebuild_from_store(store)
    session_lifecycle = SessionLifecycle()
```

- [ ] **Step 3: Run the full server test suite**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest -x`
Expected: all 609 tests pass (no new tests in this task; the smoke is that the wiring doesn't break the suite).

- [ ] **Step 4: Smoke-import the script (per the `run-gateway-smoke-test` project memory)**

Per `~/.claude/projects/-Users-kevin-Projects-Sense/memory/run-gateway-smoke-test.md`: `pytest` does not import `scripts/run_gateway.py`. Run a one-off import smoke to catch the `SystemClock` / `Protocol` / `StrictCommandValidator` class of bug that bit us earlier:

```bash
cd /Users/kevin/Projects/Sense
python -c "import ast; ast.parse(open('server/scripts/run_gateway.py').read()); print('parsed OK')"
```

Expected: `parsed OK`

(We don't run the script's `main()` because it needs `SENSE_LLM_MODEL` + `SENSE_EMBED_MODEL` set; just AST-parsing confirms the file is syntactically valid after the edit. A future CI step could do this; the project memory recommends manual smoke after every edit.)

- [ ] **Step 5: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/scripts/run_gateway.py
git commit -m "fix(server): rebuild SessionIndex on gateway startup

The in-memory SessionIndex is now populated from the durable
SqliteEventStore before the HTTP server accepts its first request.
Without this, every session captured before the latest restart
returned 404 on /sessions/{id} and /sessions/{id}/events. The
comment claiming 'a restart rebuilds the index from the durable
store on demand' is no longer a lie."
```

---

## Task 4: Server — bump the LLM system prompt to v2 with the command option

**Files:**
- Modify: `server/src/sense_server/agent/context.py` (rename constant, add the `issue_command` section, bump version string)
- Modify: `server/tests/agent/test_context.py` (update one test, add one test)

**Interfaces:**
- Consumes: the existing `_V1_SYSTEM_PROMPT` and `ContextBuilder` class. The LLM-facing prompt shape.
- Produces: a new `_V2_SYSTEM_PROMPT` constant that documents `issue_command` and the 5 valid command types. `ContextBuilder.system_prompt_version = "v2"`. The existing test pinning `"v1"` is updated to pin `"v2"`. A new test asserts the v2 prompt contains the right substrings.

- [ ] **Step 1: Update the version-pinning test to expect v2**

In `server/tests/agent/test_context.py`, find the test `test_context_builder_marks_prompt_versions` (around line 30-35). Change the assertions from:

```python
    assert prompt.system_prompt_version == "v1"
    assert prompt.context_builder_version == "v1"
```

to:

```python
    assert prompt.system_prompt_version == "v2"
    assert prompt.context_builder_version == "v1"
```

(`context_builder_version` stays "v1" — the prompt-building logic didn't change, only the prompt content.)

- [ ] **Step 2: Run the test and confirm it fails**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/agent/test_context.py::test_context_builder_marks_prompt_versions -v`
Expected: `AssertionError: assert 'v1' == 'v2'`

- [ ] **Step 3: Add the failing content test**

Append to `server/tests/agent/test_context.py`:

```python
def test_context_builder_v2_prompt_documents_command_option():
    """The v2 system prompt must mention issue_command AND every one
    of the 5 valid command types so the LLM knows the option exists
    and produces payloads the CommandValidator will accept."""
    rc = RetrievedContext(atoms=(
        _atom("a1", "x"),
    ))
    cb = ContextBuilder()
    prompt = cb.build(UserRequest(request_id="req-test", text="x"), rc, capabilities=CapabilitySet())
    assert prompt.system_prompt_version == "v2"
    # The command option must be documented.
    assert "issue_command" in prompt.system
    # All 5 P2 command types must be in the prompt.
    for cmd_type in ("capture_photo", "record_video", "start_audio", "stop_audio", "request_buffer"):
        assert cmd_type in prompt.system, f"command type {cmd_type!r} missing from v2 prompt"
    # The LLM must know to include an idempotency_key + confidence.
    assert "idempotency_key" in prompt.system
    assert "confidence" in prompt.system
```

- [ ] **Step 4: Run the content test and confirm it fails**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/agent/test_context.py::test_context_builder_v2_prompt_documents_command_option -v`
Expected: `AssertionError: assert 'issue_command' not in ...v1 prompt...` (the v1 prompt has no "issue_command" substring).

- [ ] **Step 5: Bump the system prompt to v2**

In `server/src/sense_server/agent/context.py`, make the following changes:

(a) Rename `_V1_SYSTEM_PROMPT` to `_V2_SYSTEM_PROMPT` and insert the `issue_command` section. Find the existing constant (lines 21-47) and replace it with:

```python
_V2_SYSTEM_PROMPT = """\
You are Sense, the user's ambient memory agent. You answer questions
about what the user has said, heard, and done, using only the
retrieved memory atoms provided below. You do not invent or assume
beyond what those atoms say.

# Behavior
- If the retrieved atoms support an answer, return kind="answer" with
  text and the atom_ids you used (in [id] format).
- If the user asks the wearable to DO something that requires a
  device action, return kind="issue_command" with a typed payload.
  The 5 valid command types are:
      capture_photo    (no params) — take one photo
      record_video     (duration_s: number in [1, 30]) — record a clip
      start_audio      (no params) — start audio capture
      stop_audio       (no params) — stop audio capture
      request_buffer   (seconds: number in [1, 60]) — pull a buffer of recent audio
  Each issue_command payload must carry an idempotency_key (a short
  string derived from the user request, e.g. "record_video_3s") so a
  re-prompt does not issue the same command twice, and a confidence
  score in [0, 1] reflecting your own certainty.
- If the retrieved atoms do NOT support an answer, return
  kind="no_memory" with empty atom_ids. Never guess.
- Cite every claim to at least one atom id. An atom id not in the
  retrieved set is a hallucination and is rejected.

# Output format
Return a single JSON object with these keys:
  kind        : "answer" | "no_memory" | "issue_command"
  text        : string (the answer, or "no relevant memory found"; empty for issue_command)
  atom_ids    : string[] (the ids you cite, in [id] format; empty for issue_command)
  confidence  : number in [0, 1] — your own confidence in the answer or command
  command     : (only for kind="issue_command") an object with keys
                  command_type   : one of the 5 types above
                  params         : the per-type parameter object (omit if no params)
                  idempotency_key: a short string, unique to this user request
  Note: "answer" and "no_memory" must NOT carry a "command" field.
  "issue_command" must NOT carry "text" or "atom_ids" (the structured
  payload replaces the free-form text).

# Device capabilities
{capabilities}

# Retrieved memory atoms
{atoms_block}
"""
```

(b) Bump the `ContextBuilder.system_prompt_version` class attribute (line 64) from `"v1"` to `"v2"`.

(c) Update the `build` method's `system = _V1_SYSTEM_PROMPT.format(...)` reference (line 73) to use `_V2_SYSTEM_PROMPT.format(...)`.

(d) Update the no-memory line (line 50-52) reference in `build` to use the v2 name (the constant `_V1_NO_MEMORY_LINE` is fine to keep — only the prompt template was renamed; the no-memory hint is a separate concern). Verify by searching the file for `_V1_SYSTEM_PROMPT` after the change.

- [ ] **Step 6: Run the two new tests and confirm they pass**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest tests/agent/test_context.py -v`
Expected: all tests pass (the v1 test now passes with "v2"; the new content test passes because the prompt contains all the substrings).

- [ ] **Step 7: Run the full server test suite**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest -x`
Expected: all 610 tests pass (605 + 4 rebuild + 1 prompt content).

- [ ] **Step 8: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add server/src/sense_server/agent/context.py \
        server/tests/agent/test_context.py
git commit -m "feat(agent): v2 system prompt documents issue_command

The v1 prompt told the LLM it could return kind='answer' or
kind='no_memory' and stopped there. Even though Planner._dispatch_command
was fully implemented and IssueCommandPayload accepted 5 command
types, the LLM had no idea the option existed. Asking 'record a 3
second video' got kind='no_memory' because the prompt's only
positive path was 'answer'.

v2 inserts the issue_command section between # Behavior and
# Output format, enumerates the 5 valid command types, and
documents the idempotency_key + confidence requirements. The
per-type param schemas mirror agent/validator_command.py so the
LLM produces payloads the validator will accept.

system_prompt_version bumps from 'v1' to 'v2'; audit and replay
entries produced under v1 remain valid. context_builder_version
stays 'v1' (the building logic didn't change, only the prompt
content).

1 test updated (version pin), 1 test added (content). 610 server
tests green."
```

---

## Task 5: Verify — server + Android full suites green

**Files:** none — verification only.

- [ ] **Step 1: Run the server's full test suite**

Run: `cd /Users/kevin/Projects/Sense/server && .venv/bin/python -m pytest -x`
Expected: all 610 tests pass.

- [ ] **Step 2: AST-parse `run_gateway.py` to confirm no syntax errors**

```bash
cd /Users/kevin/Projects/Sense
python -c "import ast; ast.parse(open('server/scripts/run_gateway.py').read()); print('parsed OK')"
```

Expected: `parsed OK`

- [ ] **Step 3: Run the Android test suite (unaffected, sanity check)**

Run: `cd /Users/kevin/Projects/Sense/android/sense-relay && ./gradlew testDebugUnitTest`
Expected: 244 tests pass.

- [ ] **Step 4: Update the project-status memory**

Edit `~/.claude/projects/-Users-kevin-Projects-Sense/memory/project-status.md` to reflect the closed slice. Replace the "What remains" list with a shorter list — P3 deep-link follow-up, AtomDetail deep-link, SetupActivity INV-11 tech debt, run_gateway.py CI smoke step.

- [ ] **Step 5: Final commit (memory update only)**

```bash
cd /Users/kevin/.claude/projects/-Users-kevin-Projects-Sense
git -C /Users/kevin/Projects/Sense add /Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/project-status.md
git -C /Users/kevin/Projects/Sense commit -m "docs: project-status 2026-07-20 — close the session-index-cold-start slice"
```

(If the project memory lives in a separate git repo, skip the commit — the memory index is updated in place. Verify by running `ls /Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/.git` first.)

---

## Self-Review

- **Spec coverage:** §2.1 SessionIndex rebuild → Tasks 2 + 3. §2.2 system prompt v2 → Task 4. §3 data flow (cold start) → Task 3. §4 error handling (failure isolation, empty store) → Task 2 steps 7-10. §5 testing → Task 2 (4 rebuild tests), Task 4 (1 updated, 1 new prompt test). §6 out of scope → respected (no Android changes, no proactive transcript, no BLE status). §7 design principles → preserved (no new modules, no new wire types). §8 files → every file named in exactly one task. §9 verification gate → Task 5. **M4.3 commit (Task 1)** is the spec's introduction + §2.1 dependency; spec calls for committing it.
- **Placeholders:** none. (Every step has a code block or a concrete command.)
- **Type consistency:** `SessionIndex.rebuild_from_store(event_store: "EventStore")` (Task 2) is called with `event_store=store` (Task 3). `ContextBuilder.system_prompt_version = "v2"` (Task 4) is asserted by `assert prompt.system_prompt_version == "v2"` (Task 4). `EventStore.sessions()` (Task 1's M4.3 commit) is the dependency Task 2's `rebuild_from_store` uses.
- **One sequencing constraint:** Task 1 must land before Task 2 because `rebuild_from_store` calls `event_store.sessions()` (added in the M4.3 commit). The plan sequences them in order.
- **One subtle bug I caught in the failure-isolation test:** the test as written uses `store.events = _flaky_events` (a method monkey-patch on the instance). If the `EventStore` Protocol is treated strictly, monkey-patching might not bind correctly on the in-memory impl because the impl class is concrete. The test type-ignores the assignment (`# type: ignore[method-assign]`) and is verified to pass at Task 2 step 10; if it fails on a different Python version, the fix is to create a tiny `FlakyEventStore` subclass instead of monkey-patching the instance. The plan calls this out in the test code.
