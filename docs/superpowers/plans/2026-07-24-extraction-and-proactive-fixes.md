# Extraction + Proactive Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop the live `extraction_parse_failed` and `proactive_plan_timeout` warnings by making the extraction LLM emit the contracted shape, bounding the parse-failure retry storm, fixing metric conflation, and making the proactive plan timeout model-grounded.

**Architecture:** Four small, independent, TDD changes. (1) Strengthen the extraction prompt so `qwen2.5:7b` emits `{"kind","text"}` objects instead of positional 2-string arrays. (2) In `ExtractionWorker.process_session`, log the parse-error detail and dead-letter a batch after N consecutive parse failures so a stuck cursor can't storm forever. (3) Stop `_safe_process`/`reconcile` from double-counting parse failures as indexing failures. (4) Make the proactive plan timeout env-configurable and raise the default from 2.0s (under a cold 7B model's load time) to 8.0s.

**Tech Stack:** Python 3, asyncio, pytest, sqlite3, OpenAI-compatible LLM via Ollama.

## Root-cause evidence (gathered live against the running Ollama)

- `qwen2.5:7b` on the current `DEFAULT_PROMPT` replies `["fact", "Sarah is moving to Austin next month"]` — a positional 2-string array the strict parser rejects (`LLMParseError`). Same prompt + an explicit object schema/example yields `[{"kind":"fact","text":"…"}]` ✅.
- `events.db`: 1297 events / 20 sessions. `atoms.db`: **0 atoms**. One session is stuck with the cursor 459 events behind (the retry storm); 19 advanced with 0 atoms (whisper-noise transcripts → valid `[]`).
- Cold `qwen2.5:7b` call = 2.14s (model load), already over the hardcoded `plan_timeout_s=2.0`. Warm = ~0.43s.

## Global Constraints

- Model/provider agnostic (per memory `model-provider-agnostic`): never hardcode a model; the prompt fix must not reference a specific backend. JSON mode / `response_format` is **out of scope** for this plan (provider-specific) — the prompt-strengthening alone is the proven, provider-agnostic fix.
- SQLite stores use `check_same_thread=False` + `threading.Lock` (per memory `sqlite-thread-safety-pattern`); the dead-letter counter is **in-memory only** (no schema migration) to keep this change small.
- `scripts/run_gateway.py` is not imported by pytest (per memory `run-gateway-smoke-test`); wiring changes there are verified by the unit-tested helper + a manual smoke test, not a unit test.
- Tests run from `server/` with the venv active: `cd server && python -m pytest <path> -v`.
- Commit messages end with `Co-Authored-By: Claude <noreply@anthropic.com>`.

---

### Task 1: Strengthen the extraction prompt

**Files:**
- Modify: `server/src/sense_server/memory/extract.py:41-47` (`DEFAULT_PROMPT`)
- Test: `server/tests/memory/test_extract.py`

**Interfaces:**
- Produces: an updated `DEFAULT_PROMPT` constant (same name, same string type) consumed unchanged by `LLMExtractor.__init__`. No signature changes.

**Why:** `qwen2.5:7b` ignores the current prompt's "object with fields kind and text" phrasing and emits `["fact","…"]`. Adding an explicit example + a "never return a flat array of strings" guardrail makes it conform (verified live). The strict parser stays unchanged as the backstop.

- [ ] **Step 1: Write the failing test**

Append to `server/tests/memory/test_extract.py`:

```python
def test_default_prompt_pins_object_schema_against_positional_arrays():
    """The prompt must instruct the model to emit {kind,text} OBJECTS, not
    positional 2-string arrays. qwen2.5:7b emits ["fact","..."] without this
    guidance, which the strict parser rejects (extraction_parse_failed) and
    the cursor sticks (the 459-events-behind storm). This pins the fix against
    a regression that silently re-weakens the prompt.
    """
    from sense_server.memory.extract import DEFAULT_PROMPT

    assert '"kind"' in DEFAULT_PROMPT
    assert '"text"' in DEFAULT_PROMPT
    assert "flat array of strings" in DEFAULT_PROMPT
    # An explicit worked example anchors the shape for small chat-tuned models.
    assert '[{"kind"' in DEFAULT_PROMPT
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/memory/test_extract.py::test_default_prompt_pins_object_schema_against_positional_arrays -v`
Expected: FAIL — `"flat array of strings"` and `[{"kind"` are not in the current prompt.

- [ ] **Step 3: Write minimal implementation**

Replace the `DEFAULT_PROMPT` assignment in `server/src/sense_server/memory/extract.py` (lines 41-47) with:

```python
DEFAULT_PROMPT = (
    "You extract durable, factual memories from a short transcript of someone's day. "
    "Respond with ONLY a JSON array — no prose, no code fences. Each element MUST be a "
    "JSON object with exactly two string fields: \"kind\" (one of: fact, task, "
    "preference, event) and \"text\" (a concise, self-contained memory written in the "
    'third person). Example: [{"kind":"fact","text":"Sarah is moving to Austin next '
    'month."}]. Never return a flat array of strings like ["fact","some text"] — each '
    "element must be an object with \"kind\" and \"text\" keys. If nothing is worth "
    "remembering, return []."
)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd server && python -m pytest tests/memory/test_extract.py -v`
Expected: PASS (new test + all existing strict-parser tests still green).

- [ ] **Step 5: Commit**

```bash
cd server && git add src/sense_server/memory/extract.py tests/memory/test_extract.py
git commit -m "fix(memory): strengthen extraction prompt to force {kind,text} objects

qwen2.5:7b ignored the old phrasing and emitted positional 2-string arrays
([\"fact\",\"...\"]), which the strict parser rejects (extraction_parse_failed)
and the cursor sticks (the live 459-events-behind storm). Add an explicit
example + a 'never return a flat array of strings' guardrail. Verified live
against the running Ollama to yield conforming output.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Log parse-error detail + dead-letter the retry storm

**Files:**
- Modify: `server/src/sense_server/contracts/metrics.py` (add `EXTRACTION_DEAD_LETTER_TOTAL`)
- Modify: `server/src/sense_server/memory/extraction_worker.py` (constructor `__init__`, `process_session`)
- Test: `server/tests/memory/test_extraction_worker.py`

**Interfaces:**
- Consumes: `Metrics.EXTRACTION_DEAD_LETTER_TOTAL` (new constant added in this task).
- Produces: `ExtractionWorker(..., max_parse_failures: int = 5)` — new optional constructor kwarg (default preserves all existing callers). `process_session` now returns `[]` (no raise) when a batch is dead-lettered; below the cap it still re-raises `LLMParseError` (unchanged behavior).

**Why:** (a) `extraction_parse_failed` logs only `session_id` — the parse error and reply shape are discarded, so the failure is unactionable. (b) On `LLMParseError` the cursor never advances (H7), so every new event re-runs the whole growing backlog — the stuck session is 459 events behind. After N consecutive parse failures on the same session, advance the cursor past the stuck batch (dead-letter), log + count it. Events remain in `events.db` for re-ingestion after a prompt/model fix.

- [ ] **Step 1: Add the new metric constant**

In `server/src/sense_server/contracts/metrics.py`, add next to `LLM_PARSE_FAILURES_TOTAL`:

```python
    EXTRACTION_DEAD_LETTER_TOTAL:   Final = "extraction_dead_letter_total"
```

- [ ] **Step 2: Write the failing tests**

Append to `server/tests/memory/test_extraction_worker.py`:

```python
def test_worker_logs_parse_error_detail_on_parse_failure(caplog):
    """extraction_parse_failed must carry the parse error string (which
    includes the raw reply shape) so an operator can see WHY extraction
    failed. Without it the warning is unactionable.
    """
    import logging
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    metrics = InMemoryMetricsRecorder()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(events=events, atoms=atoms, pipeline=pipeline, metrics=metrics)
    with caplog.at_level(logging.WARNING, logger="sense_server.memory.extraction_worker"):
        with pytest.raises(LLMParseError):
            w.process_session("s1")
    records = [r for r in caplog.records if "extraction_parse_failed" in r.message]
    assert records, "expected an extraction_parse_failed warning"
    # The MalformedExtractor raises LLMParseError("malformed extraction reply: ...").
    assert "malformed extraction reply" in getattr(records[0], "error", "")


def test_worker_dead_letters_after_max_consecutive_parse_failures():
    """After N consecutive parse failures on the same session, the worker
    advances the cursor past the stuck batch (dead-letter) and stops
    retrying it on every new event. Without this, a structurally
    non-conforming model leaves the cursor stuck forever and re-runs the
    whole backlog on every enqueue (the 459-events-behind storm).
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    events.append(_event(1))
    metrics = InMemoryMetricsRecorder()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics, max_parse_failures=3,
    )
    # First N-1 attempts raise and leave the cursor stuck.
    for _ in range(2):
        with pytest.raises(LLMParseError):
            w.process_session("s1")
    assert atoms.get_cursor("s1") == -1
    # Nth attempt dead-letters: no raise, cursor advances past the batch.
    result = w.process_session("s1")
    assert result == []
    assert atoms.get_cursor("s1") == 1
    assert metrics.counter(
        Metrics.EXTRACTION_DEAD_LETTER_TOTAL, tags={"session_id": "s1"}
    ) == 1
    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 3


def test_worker_resets_parse_failure_count_on_success():
    """A successful extraction resets the consecutive-failure counter, so a
    later transient failure does not immediately dead-letter a batch that
    had previously failed a few times then recovered.
    """
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    for s in (0, 1, 2, 3):
        events.append(_event(s))
    metrics = InMemoryMetricsRecorder()

    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(
        events=events, atoms=atoms, pipeline=pipeline,
        metrics=metrics, max_parse_failures=3,
    )
    # Two consecutive failures: count=2, cursor stuck.
    for _ in range(2):
        with pytest.raises(LLMParseError):
            w.process_session("s1")
    assert atoms.get_cursor("s1") == -1
    # Fix the extractor; success advances the cursor and resets the counter.
    pipeline._extraction = ExtractionStage(extractor=FixedExtractor(), clock=clock)
    w.process_session("s1")
    assert atoms.get_cursor("s1") == 3
    # More events land; extractor breaks again. Only 1 failure since the
    # reset, so it must raise (not dead-letter) and leave the cursor put.
    events.append(_event(4))
    events.append(_event(5))
    pipeline._extraction = ExtractionStage(extractor=MalformedExtractor(), clock=clock)
    with pytest.raises(LLMParseError):
        w.process_session("s1")
    assert atoms.get_cursor("s1") == 3
    assert metrics.counter(
        Metrics.EXTRACTION_DEAD_LETTER_TOTAL, tags={"session_id": "s1"}
    ) == 0
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/memory/test_extraction_worker.py::test_worker_logs_parse_error_detail_on_parse_failure tests/memory/test_extraction_worker.py::test_worker_dead_letters_after_max_consecutive_parse_failures tests/memory/test_extraction_worker.py::test_worker_resets_parse_failure_count_on_success -v`
Expected: FAIL — `ExtractionWorker` has no `max_parse_failures` kwarg; `Metrics.EXTRACTION_DEAD_LETTER_TOTAL` does not exist; `error` is not attached to the log record.

- [ ] **Step 4: Implement the constructor change**

In `server/src/sense_server/memory/extraction_worker.py`, update `ExtractionWorker.__init__` to add the kwarg and the in-memory counter. Add `max_parse_failures: int = 5` as the last parameter, and after the `self._listeners = ...` line add:

```python
        # In-memory consecutive-parse-failure counter for dead-lettering.
        # Resets on a successful extraction; lives only in memory (a
        # restart re-collects up to max_parse_failures failures before
        # dead-lettering again). Kept out of the schema to avoid a
        # migration for this bounded, transient state.
        self._parse_failures: dict[str, int] = {}
        self._max_parse_failures = max_parse_failures
```

- [ ] **Step 5: Implement the `process_session` restructure**

Replace the body of `process_session` in `server/src/sense_server/memory/extraction_worker.py` (from `start = time.monotonic()` through the final `return indexed`) with:

```python
        start = time.monotonic()
        # 1. read events past the cursor
        cursor = self._atoms.get_cursor(session_id)
        all_events = self._events.events(session_id)
        pending = [e for e in all_events if e.seq > cursor]
        if not pending:
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            return []
        last_pending_seq = max(e.seq for e in pending)
        # 2. run the pipeline (extract -> version -> embed -> index).
        #    The pipeline may raise; the cursor must NOT advance — unless
        #    this is the Nth consecutive parse failure (dead-letter, below).
        try:
            indexed = self._pipeline.run(session_id, pending)
        except LLMParseError as exc:
            # Parse failure is structurally distinct from an indexing
            # failure. Count it on its own metric (tagged with the
            # session id) and re-raise so the next enqueue / reconciliation
            # retries the same events. The cursor was not advanced.
            # NOTE: do NOT also increment INDEXING_FAILURES_TOTAL here
            # — the two counters must never overlap or the dashboard
            # conflates "LLM misbehaved" with "embedder/indexer crashed".
            self._metrics.increment(
                Metrics.LLM_PARSE_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            self._parse_failures[session_id] = (
                self._parse_failures.get(session_id, 0) + 1
            )
            if self._parse_failures[session_id] >= self._max_parse_failures:
                # Dead-letter: advance past the stuck batch so the worker
                # stops re-running it on every new enqueue. The events
                # remain in the event store for re-ingestion after a
                # prompt/model fix; this only bounds the retry storm.
                self._atoms.set_cursor(session_id, last_pending_seq)
                self._parse_failures.pop(session_id, None)
                self._metrics.increment(
                    Metrics.EXTRACTION_DEAD_LETTER_TOTAL,
                    tags={"session_id": session_id},
                )
                log.warning(
                    "extraction_dead_lettered",
                    extra={
                        "session_id": session_id,
                        "advanced_to": last_pending_seq,
                        "batch_size": len(pending),
                        "attempts": self._max_parse_failures,
                        "error": str(exc),
                    },
                )
                return []
            log.warning(
                "extraction_parse_failed",
                extra={"session_id": session_id, "error": str(exc)},
            )
            raise
        except Exception:
            self._metrics.increment(
                Metrics.INDEXING_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            raise
        # 3. advance the cursor to the last event seq we just processed.
        #    H7: only after extraction AND indexing have both succeeded.
        self._atoms.set_cursor(session_id, last_pending_seq)
        # A successful extraction resets the consecutive-failure counter.
        self._parse_failures.pop(session_id, None)
        # 4. P3: dispatch to listeners after the cursor advances.
        if pending:
            completion = SessionCompletion(
                session_id=session_id,
                completed_at=datetime.now(tz=timezone.utc),
                event_id_range=(pending[0].seq, pending[-1].seq),
            )
            self._dispatch_listeners(completion)
        self._metrics.observe(
            Metrics.EXTRACTION_LATENCY_MS,
            (time.monotonic() - start) * 1000.0,
        )
        return indexed
```

Also extend the `process_session` docstring with this paragraph (append before the `Returns` line):

```python
        A *persistent* parse failure is dead-lettered: after
        ``max_parse_failures`` consecutive :class:`LLMParseError` on the
        same session, the cursor advances past the stuck batch so the
        worker stops re-running it on every new event (which would
        re-attempt the whole growing backlog each time). The events
        remain in the event store for re-ingestion after a prompt/model
        fix. Counted as :data:`Metrics.EXTRACTION_DEAD_LETTER_TOTAL` and
        logged as ``extraction_dead_lettered``.
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/memory/test_extraction_worker.py -v`
Expected: PASS — all three new tests pass AND the existing H7 / parse-failure / self-healing tests stay green (default `max_parse_failures=5` means a single failure still raises and leaves the cursor put).

- [ ] **Step 7: Commit**

```bash
cd server && git add src/sense_server/contracts/metrics.py src/sense_server/memory/extraction_worker.py tests/memory/test_extraction_worker.py
git commit -m "fix(memory): log parse-error detail + dead-letter the retry storm

extraction_parse_failed now carries the parse error string (which includes
the raw reply shape) so the failure is actionable. After max_parse_failures
(default 5) consecutive LLMParseErrors on the same session, the cursor
advances past the stuck batch (dead-letter) so a structurally non-conforming
model can't leave the worker re-running the whole backlog on every new
event (the live 459-events-behind storm). Events stay in the event store
for re-ingestion after a prompt/model fix. New EXTRACTION_DEAD_LETTER_TOTAL
metric + extraction_dead_lettered log. H7 preserved: cursor advances only
after the full pipeline succeeds (or a deliberate dead-letter).

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: Stop double-counting parse failures as indexing failures

**Files:**
- Modify: `server/src/sense_server/memory/extraction_worker.py` (`_safe_process` ~line 606, `reconcile` ~line 532)
- Test: `server/tests/memory/test_extraction_worker.py`

**Interfaces:**
- Produces: no signature changes. `_safe_process` and `reconcile` now catch `LLMParseError` separately (re-raise / continue without incrementing `INDEXING_FAILURES_TOTAL`).

**Why:** `process_session` increments `LLM_PARSE_FAILURES_TOTAL` and re-raises. The `_safe_process` wrapper (the production `_run` path) catches the re-raised `LLMParseError` as `except Exception` and ALSO increments `INDEXING_FAILURES_TOTAL` — the exact conflation the `process_session` docstring says must never happen. `reconcile` does the same. The dashboard reads "embedder/indexer crashed" for what is really "LLM misbehaved."

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/memory/test_extraction_worker.py`:

```python
def _malformed_pipeline(metrics, clock):
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    return events, atoms, pipeline


def test_safe_process_does_not_count_parse_failure_as_indexing_failure():
    """The _run-path wrapper must not double-count a parse failure as
    INDEXING_FAILURES_TOTAL — process_session already counted it as
    LLM_PARSE_FAILURES_TOTAL. The dashboard must not conflate the two.
    """
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    events, atoms, pipeline = _malformed_pipeline(InMemoryMetricsRecorder(), clock)
    metrics = InMemoryMetricsRecorder()
    # Rebuild with the metrics recorder we assert against.
    events = InMemoryEventStore()
    atoms2 = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms2,
    )
    w = ExtractionWorker(events=events, atoms=atoms2, pipeline=pipeline, metrics=metrics)
    with pytest.raises(LLMParseError):
        w._safe_process("s1")
    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 1
    assert metrics.counter(
        Metrics.INDEXING_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 0


def test_reconcile_does_not_count_parse_failure_as_indexing_failure():
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    events = InMemoryEventStore()
    atoms = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    events.append(_event(0))
    metrics = InMemoryMetricsRecorder()
    pipeline = Pipeline(
        extraction=ExtractionStage(extractor=MalformedExtractor(), clock=clock),
        version_stamp=VersionStampStage(),
        embedding=EmbeddingStage(embedder=HappyEmbedder()),
        indexing=IndexingStage(index=idx),
        store=atoms,
    )
    w = ExtractionWorker(events=events, atoms=atoms, pipeline=pipeline, metrics=metrics)
    w.reconcile()  # swallows per-session failures
    assert metrics.counter(
        Metrics.LLM_PARSE_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 1
    assert metrics.counter(
        Metrics.INDEXING_FAILURES_TOTAL, tags={"session_id": "s1"}
    ) == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/memory/test_extraction_worker.py::test_safe_process_does_not_count_parse_failure_as_indexing_failure tests/memory/test_extraction_worker.py::test_reconcile_does_not_count_parse_failure_as_indexing_failure -v`
Expected: FAIL — both assert `INDEXING_FAILURES_TOTAL == 0` but the wrappers currently increment it to 1.

- [ ] **Step 3: Implement the `_safe_process` fix**

In `server/src/sense_server/memory/extraction_worker.py`, replace the `except Exception:` block inside `_safe_process` with a parse-failure-specific catch first:

```python
    def _safe_process(self, session_id: str) -> None:
        """Wrap :meth:`process_session` in latency + error metrics."""
        start = time.monotonic()
        try:
            self.process_session(session_id)
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
        except LLMParseError:
            # Already counted as LLM_PARSE_FAILURES_TOTAL (and latency
            # observed) inside process_session. Do NOT also count it as
            # an indexing failure — the two counters must never overlap
            # or the dashboard conflates "LLM misbehaved" with
            # "embedder/indexer crashed".
            raise
        except Exception:
            self._metrics.increment(
                Metrics.INDEXING_FAILURES_TOTAL,
                tags={"session_id": session_id},
            )
            self._metrics.observe(
                Metrics.EXTRACTION_LATENCY_MS,
                (time.monotonic() - start) * 1000.0,
            )
            # Re-raise for the reconcile path; swallow for the queue path.
            raise
```

- [ ] **Step 4: Implement the `reconcile` fix**

In `reconcile`, replace the per-session `try/except` with a parse-failure-specific catch:

```python
        for sid in sorted(sessions):
            try:
                indexed.extend(self.process_session(sid))
            except LLMParseError:
                # Already counted as LLM_PARSE_FAILURES_TOTAL in
                # process_session. Don't double-count as an indexing
                # failure; just skip this session and keep going.
                continue
            except Exception:
                self._metrics.increment(
                    Metrics.INDEXING_FAILURES_TOTAL,
                    tags={"session_id": sid},
                )
                # Swallow: failure isolation — keep going.
                continue
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/memory/test_extraction_worker.py -v`
Expected: PASS — both new tests pass and the full worker suite stays green.

- [ ] **Step 6: Commit**

```bash
cd server && git add src/sense_server/memory/extraction_worker.py tests/memory/test_extraction_worker.py
git commit -m "fix(memory): don't double-count parse failures as indexing failures

_safe_process (the _run-path wrapper) and reconcile caught the re-raised
LLMParseError as a generic Exception and incremented INDEXING_FAILURES_TOTAL,
conflating 'LLM misbehaved' with 'embedder/indexer crashed' on the dashboard
— the exact overlap the process_session docstring says must never happen.
Catch LLMParseError separately and let process_session's own
LLM_PARSE_FAILURES_TOTAL be the sole counter.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Make the proactive plan timeout env-configurable + raise the default

**Files:**
- Modify: `server/src/sense_server/agent/proactive.py` (add helper + constants)
- Modify: `server/scripts/run_gateway.py:221-241` (use the helper)
- Test: `server/tests/agent/test_proactive_engine.py`

**Interfaces:**
- Produces: `plan_timeout_from_env(env, default=DEFAULT_PLAN_TIMEOUT_S) -> float` and constants `DEFAULT_PLAN_TIMEOUT_S = 8.0`, `ENV_PROACTIVE_PLAN_TIMEOUT_S = "SENSE_PROACTIVE_PLAN_TIMEOUT_S"` in `sense_server.agent.proactive`. `ProactiveTriggerEngine` itself is unchanged (already takes `plan_timeout_s`).

**Why:** `plan_timeout_s=2.0` is hardcoded in `run_gateway.py`. A cold `qwen2.5:7b` load takes ~2.1s, so every cold proactive call times out before generating — `proactive_plan_timeout`. 8.0s fits a cold load + generation on modest hardware while still bounding a stuck planner. Making it env-configurable lets operators tune per-model without a code change.

- [ ] **Step 1: Write the failing tests**

Append to `server/tests/agent/test_proactive_engine.py`:

```python
def test_plan_timeout_from_env_defaults_to_8s():
    from sense_server.agent.proactive import (
        plan_timeout_from_env, DEFAULT_PLAN_TIMEOUT_S,
    )
    assert DEFAULT_PLAN_TIMEOUT_S == 8.0
    assert plan_timeout_from_env({}) == 8.0


def test_plan_timeout_from_env_reads_override():
    from sense_server.agent.proactive import plan_timeout_from_env
    assert plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "5.0"}) == 5.0


def test_plan_timeout_from_env_rejects_nonpositive_and_nonnumeric():
    import pytest
    from sense_server.agent.proactive import plan_timeout_from_env
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "0"})
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "-1"})
    with pytest.raises(ValueError):
        plan_timeout_from_env({"SENSE_PROACTIVE_PLAN_TIMEOUT_S": "notanum"})
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/agent/test_proactive_engine.py::test_plan_timeout_from_env_defaults_to_8s tests/agent/test_proactive_engine.py::test_plan_timeout_from_env_reads_override tests/agent/test_proactive_engine.py::test_plan_timeout_from_env_rejects_nonpositive_and_nonnumeric -v`
Expected: FAIL — `plan_timeout_from_env` / `DEFAULT_PLAN_TIMEOUT_S` do not exist (ImportError).

- [ ] **Step 3: Implement the helper**

In `server/src/sense_server/agent/proactive.py`, add after the module docstring / imports (before the `WsSender` protocol):

```python
DEFAULT_PLAN_TIMEOUT_S = 8.0
ENV_PROACTIVE_PLAN_TIMEOUT_S = "SENSE_PROACTIVE_PLAN_TIMEOUT_S"


def plan_timeout_from_env(env: Mapping[str, str], default: float = DEFAULT_PLAN_TIMEOUT_S) -> float:
    """Read ``SENSE_PROACTIVE_PLAN_TIMEOUT_S``, validating it is > 0.

    The previous hard-coded 2.0s was an SLA, not a model-grounded budget:
    a cold local 7B model loads in ~2.1s, so every cold proactive call
    timed out before generating (``proactive_plan_timeout``). 8.0s fits a
    cold load + generation on modest hardware while still bounding a
    stuck planner. Operators can lower/raise it per model without a
    code change.
    """
    raw = env.get(ENV_PROACTIVE_PLAN_TIMEOUT_S)
    if not raw:
        return default
    try:
        val = float(raw)
    except ValueError as e:
        raise ValueError(
            f"{ENV_PROACTIVE_PLAN_TIMEOUT_S}={raw!r} is not a valid float"
        ) from e
    if val <= 0:
        raise ValueError(f"{ENV_PROACTIVE_PLAN_TIMEOUT_S}={val} must be > 0")
    return val
```

Add `Mapping` to the imports from `typing` (change `from typing import Protocol, runtime_checkable` to `from typing import Mapping, Protocol, runtime_checkable`).

- [ ] **Step 4: Wire it into the gateway**

In `server/scripts/run_gateway.py`, update the import (around line 221):

```python
    from sense_server.agent.proactive import ProactiveTriggerEngine, plan_timeout_from_env
```

And change the engine construction (around line 234-241) to:

```python
    proactive_engine = ProactiveTriggerEngine(
        planner=planner,
        ws_sender=_PlaceholderWsSender(),
        clock=SystemClock(),
        metrics=metrics,
        ids=UuidIdGenerator(),
        plan_timeout_s=plan_timeout_from_env(__import__("os").environ),
    )
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd server && python -m pytest tests/agent/test_proactive_engine.py -v`
Expected: PASS — three new tests pass, existing proactive-engine tests unchanged (they pass `plan_timeout_s` directly).

- [ ] **Step 6: Commit**

```bash
cd server && git add src/sense_server/agent/proactive.py scripts/run_gateway.py tests/agent/test_proactive_engine.py
git commit -m "fix(agent): make proactive plan timeout env-configurable, default 8s

The hardcoded 2.0s was under a cold qwen2.5:7b model's ~2.1s load time, so
every cold proactive call timed out before generating (proactive_plan_timeout).
Add SENSE_PROACTIVE_PLAN_TIMEOUT_S (default 8.0s, validated > 0) and read it
in run_gateway. The engine already accepted plan_timeout_s as a constructor
arg, so no engine change is needed.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Final verification (after all 4 tasks)

- [ ] **Run the full server suite**

Run: `cd server && python -m pytest -q`
Expected: all green (memory: ~640 tests pre-change; this plan adds 8 new tests, no removals).

- [ ] **Live smoke test against the running Ollama**

1. Confirm the new prompt produces conforming output:
   ```bash
   cd /Users/kevin/Projects/Sense/server && python -c "
   from sense_server.memory.extract import LLMExtractor, DEFAULT_PROMPT
   from sense_server.memory.llm import OpenAICompatibleChatModel
   import os
   m = OpenAICompatibleChatModel.from_env(os.environ)
   print(LLMExtractor(m).extract('I had coffee with Sarah at Blue Bottle. She is moving to Austin next month.'))
   "
   ```
   Expected: a list of `ExtractedMemory(kind=..., text=...)` (no `LLMParseError`). Run with `set -a && source ../.env && set +a` first so `SENSE_LLM_*` are in the env.

2. Restart the gateway (`python scripts/run_gateway.py`) and confirm: (a) no `extraction_parse_failed` on a real memorable transcript; (b) the stuck session's 459-event backlog now extracts (cursor advances) because the prompt conforms; (c) `proactive_plan_timeout` stops on warm calls (and a cold first call now fits within 8s). Watch `atoms.db` atom count rise above 0.

- [ ] **Update memory**

Update `MEMORY.md` index + add/replace the `llm-extractor-silent-loss` and `active-plan` memories to record: prompt-strengthening fixed `extraction_parse_failed`; dead-letter bounds the storm; 2s→8s configurable timeout fixed `proactive_plan_timeout`.

---

## Out of scope (follow-ups, not in this plan)

- Move the retriever's embedder call off the event loop (`await asyncio.to_thread` in `Planner._do_retrieve`) — real loop-blocking but not the reported warning; separate change in the most-tested component.
- Empty-index short-circuit in the proactive engine (skip the planner LLM call when retrieval is empty) — optimization, needs a new seam.
- Provider-specific JSON mode (`response_format`/Ollama `format`) — provider-agnostic prompt fix is sufficient for now.
- Dedicated/lighter proactive model or Ollama pre-warm — operational, not code.