# Extraction Windowing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Aggregate 1-second event fragments into 60s windows before LLM extraction so the model sees coherent conversation and memories form.

**Architecture:** The streaming transcriber emits one transcript per 1-second audio hop, and each becomes a `CaptureEvent`. `ExtractionStage` currently calls the extractor **once per event**, so the LLM sees fragments in isolation (`"and send it to my phone now."`) and returns `[]`. The fix groups ordered events into time windows (≤ `window_ms` span from the first event's `start_ms`), joins each window's text into one transcript, calls the extractor once per window, and attributes every resulting atom to the window's **last** event (`source_event_id`, `start_ms`, `atom_id = "{last.event_id}:{index}"`). Cursor / H7 unchanged.

**Tech Stack:** Python, pytest, existing `ExtractionStage` in `server/src/sense_server/memory/stages.py`.

## Global Constraints

- `atom_id` MUST stay deterministic per window so `AtomStore.append` (idempotent on `atom_id`) and re-runs stay safe.
- Windowing is by `start_ms` span, not wall-clock bucket, so a conversation starting mid-minute groups correctly and silence gaps just shrink a window.
- Production path only: `stages.ExtractionStage` (used by `run_gateway` → `ExtractionWorker`). Legacy `memory/pipeline.py ExtractionPipeline` (manual `extract_memories.py` script) is out of scope — same per-event bug but not on the hot path; leave it.
- Default `window_ms = 60_000`. Events are 1s hops, so a 60s window ≈ 60 hops, well under a 7B model's context.

---

## File Structure

- Modify: `server/src/sense_server/memory/stages.py` — `ExtractionStage` (windowing + per-window extract + last-event attribution).
- Modify: `server/tests/memory/test_stages.py` — update the two per-event tests to the windowed contract; add windowing tests.

---

### Task 1: Window ExtractionStage

**Files:**
- Modify: `server/src/sense_server/memory/stages.py` (`ExtractionStage`)
- Test: `server/tests/memory/test_stages.py`

**Interfaces:**
- Consumes: `Extractor.extract(text) -> list[ExtractedMemory]`, `CaptureEvent` (`.text`, `.start_ms`, `.event_id`).
- Produces: `ExtractionStage(extractor, clock, window_ms=60_000)`; `.run(session_id, events) -> list[MemoryAtom]` where atoms are attributed to each window's last event.

- [ ] **Step 1: Write the failing tests**

Replace `test_extraction_stage_produces_atoms_per_event` and `test_extraction_stage_respects_event_count` with the windowed contract, and add new tests. Append/update in `server/tests/memory/test_stages.py` (after the `_event` helper):

```python
def test_extraction_stage_joins_same_window_events_into_one_extract_call():
    """Events within one 60s window are joined into a single transcript and
    the extractor is called once. Atoms are attributed to the window's last
    event. This is the fix for per-event extraction on 1-second fragments,
    where the LLM saw each hop in isolation and returned []."""
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    stage = ExtractionStage(extractor=FixedExtractor(), clock=clock)
    atoms = stage.run("s1", [_event(0, text="so basically"), _event(1, text="I was saying")])
    # one window -> one extract call -> one atom (FixedExtractor echoes joined text)
    assert len(atoms) == 1
    assert atoms[0].text == "so basically I was saying"
    assert atoms[0].source_event_id == "s1:1"  # window's last event
    assert atoms[0].start_ms == 1000
    assert atoms[0].atom_id == "s1:1:0"  # deterministic


def test_extraction_stage_splits_events_across_windows():
    """Events whose start_ms spans past window_ms from the first event start
    a new window, each producing its own extract call."""
    def clock() -> datetime:
        return datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)
    # window_ms=2000: event 0 (0ms), 1 (1000ms) -> W1; event 2 (2000ms) -> W2
    stage = ExtractionStage(extractor=FixedExtractor(), clock=clock, window_ms=2000)
    atoms = stage.run("s1", [_event(0, text="a"), _event(1, text="b"), _event(2, text="c")])
    # two windows -> two atoms, attributed to each window's last event
    assert [a.text for a in atoms] == ["a b", "c"]
    assert [a.source_event_id for a in atoms] == ["s1:1", "s1:2"]
    assert [a.atom_id for a in atoms] == ["s1:1:0", "s1:2:0"]


def test_extraction_stage_window_ms_default_is_60s():
    assert ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)._window_ms == 60_000


def test_extraction_stage_rejects_nonpositive_window_ms():
    with pytest.raises(ValueError):
        ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED, window_ms=0)


def test_extraction_stage_empty_events_returns_empty():
    stage = ExtractionStage(extractor=FixedExtractor(), clock=lambda: FIXED)
    assert stage.run("s1", []) == []


def test_extraction_stage_event_yielding_no_memories_advances_no_atom():
    """A window whose joined text yields [] produces no atom (the cursor still
    advances in the worker; the stage just returns fewer atoms)."""
    stage = ExtractionStage(
        extractor=FixedExtractor(memories=[]), clock=lambda: FIXED,
    )
    assert stage.run("s1", [_event(0, text="noise"), _event(1, text="noise")]) == []
```

(Add `FIXED = datetime(2026, 7, 7, 0, 0, 0, tzinfo=timezone.utc)` near the top if not present, or reuse an existing clock.)

Also DELETE the now-obsolete `test_extraction_stage_produces_atoms_per_event` and `test_extraction_stage_respects_event_count` (they encode the per-event contract that is intentionally being replaced) — or rewrite them to the windowed contract above.

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && .venv/bin/python -m pytest tests/memory/test_stages.py -q`
Expected: FAIL — new windowing tests fail (no `_window_ms`, no windowing), and the two deleted per-event tests are gone.

- [ ] **Step 3: Implement windowing in ExtractionStage**

In `server/src/sense_server/memory/stages.py`, replace `ExtractionStage`:

```python
class ExtractionStage:
    """Stage 1: events -> atom candidates.

    Groups ordered events into time windows (at most ``window_ms`` of span
    from the first event's ``start_ms``), joins each window's text into one
    transcript, and calls the extractor **once per window** — not once per
    event. The streaming transcriber emits one transcript per 1-second audio
    hop, so per-event extraction fed the LLM fragments in isolation
    ("and send it to my phone now.") and it returned [] for nearly every
    hop. Windowing lets the model see a coherent ~60s slice of conversation
    and extract the memories that span multiple hops.

    Every atom produced by a window is attributed to the window's **last**
    event: ``source_event_id`` and ``start_ms`` are the last event's, and
    ``atom_id`` is ``"{last.event_id}:{index}"``. This keeps ids deterministic
    for a given set of events + window_ms, so ``AtomStore.append`` (idempotent
    on ``atom_id``) and worker re-runs stay safe. The cursor / H7 invariant is
    unchanged — the worker still advances the cursor to the last processed
    event seq only after extraction + indexing succeed.
    """

    def __init__(
        self, extractor: Extractor, clock: Clock, window_ms: int = 60_000,
    ) -> None:
        if window_ms <= 0:
            raise ValueError("window_ms must be > 0")
        self._extractor = extractor
        self._clock = clock
        self._window_ms = window_ms

    def run(
        self, session_id: str, events: Iterable[CaptureEvent]
    ) -> list[MemoryAtom]:
        now = self._clock()
        atoms: list[MemoryAtom] = []
        for window in self._windows(list(events)):
            joined = " ".join(e.text for e in window)
            last = window[-1]
            for index, memory in enumerate(self._extractor.extract(joined)):
                atoms.append(
                    MemoryAtom(
                        atom_id=f"{last.event_id}:{index}",
                        session_id=session_id,
                        source_event_id=last.event_id,
                        kind=memory.kind,
                        text=memory.text,
                        created_at=now,
                        start_ms=last.start_ms,
                    )
                )
        return atoms

    def _windows(
        self, events: list[CaptureEvent]
    ) -> list[list[CaptureEvent]]:
        """Group events ordered by ``start_ms`` into windows of at most
        ``window_ms`` span. A window starts at an event's ``start_ms`` and
        includes every following event whose ``start_ms`` is within
        ``window_ms`` of it; the next event past that boundary starts a new
        window. Silence gaps (no events) simply shrink a window."""
        windows: list[list[CaptureEvent]] = []
        current: list[CaptureEvent] = []
        window_start_ms: int | None = None
        for event in events:
            if (
                window_start_ms is None
                or event.start_ms - window_start_ms >= self._window_ms
            ):
                if current:
                    windows.append(current)
                current = [event]
                window_start_ms = event.start_ms
            else:
                current.append(event)
        if current:
            windows.append(current)
        return windows
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd server && .venv/bin/python -m pytest tests/memory/test_stages.py -q`
Expected: PASS — all windowing tests green.

- [ ] **Step 5: Run the full memory + worker suite**

Run: `cd server && .venv/bin/python -m pytest tests/memory/ -q`
Expected: PASS — pipeline/invariants/worker tests still green (production path uses `stages.ExtractionStage`; legacy `pipeline.py` tests unchanged).

- [ ] **Step 6: Run the full server suite**

Run: `cd server && .venv/bin/python -m pytest -q`
Expected: PASS — 650+ tests green, no new warnings.

- [ ] **Step 7: Commit**

```bash
git add server/src/sense_server/memory/stages.py server/tests/memory/test_stages.py
git commit -m "$(cat <<'EOF'
fix(memory): extract per 60s window, not per 1s event

The streaming transcriber emits one transcript per 1-second audio hop, and
ExtractionStage called the LLM extractor once per event — so the model saw
each hop in isolation ("and send it to my phone now.") and returned [] for
nearly every one. Memories that span multiple hops (the common case) never
formed. The DB confirms real content exists, just split across consecutive
hops (seq 5/6/7, seq 18/20).

Group ordered events into <=60s windows by start_ms span, join each window's
text into one transcript, call the extractor once per window, and attribute
atoms to the window's last event (source_event_id, start_ms, atom_id =
"{last.event_id}:{index}" — deterministic so idempotent append + re-runs stay
safe). Cursor / H7 unchanged. window_ms is configurable (default 60_000).

Legacy memory/pipeline.py (manual extract_memories.py script only) keeps
per-event extraction; out of scope.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

## Verification (post-merge, by the operator)

After restarting the gateway with the existing `events.db` backlog, the reconciliation sweep will re-extract sessions whose cursor is behind, this time over 60s windows. Memories should form from the previously-fragmented transcripts (e.g. the "record a video / take a picture" requests, the "send it to my phone" sequence). The `quarter quarter...` / `Max Max...` whisper-loop windows still yield [] or parse-fail and are handled by the existing empty-batch gate + dead-letter.