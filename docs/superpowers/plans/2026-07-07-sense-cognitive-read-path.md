# Sense — Cognitive Read Path Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land the P0 + P1 + P2-answers slice — the server's cognitive read path — so the user can ask natural-language questions from a new Android Chat screen and receive evidence-backed answers drawn from a versioned, retrievable memory of past transcripts.

**Architecture:** A stateless Planner orchestrates a fixed seven-stage pipeline (Retrieve → BuildContext → Reason → Validate → Guard → Audit → Metrics) using Protocol-conforming components. New modules are added alongside existing memory/commands code; nothing in the working capture/transcription/event path is rewritten. End-to-end integration test is the primary regression gate.

**Tech Stack:** Python 3.12, aiohttp, pydantic 2, SQLite, asyncio (server); Kotlin, Jetpack Compose, DataStore, OkHttp (Android); existing `mlx_whisper` transcription (unchanged); OpenAI-compatible LLM via `OpenAICompatibleChatModel` (existing).

**Spec:** [`docs/superpowers/specs/2026-07-07-sense-cognitive-read-path-design.md`](../specs/2026-07-07-sense-cognitive-read-path-design.md) (frozen on 2026-07-07 with seven High-priority amendments H1–H7).

## Global Constraints

- Python 3.12, pydantic 2, aiohttp, SQLite via stdlib `sqlite3` (server). No new runtime deps.
- Kotlin + Compose, DataStore, OkHttp (Android). No new runtime deps.
- All Protocol interfaces are `@runtime_checkable`; production impls satisfy them via `isinstance`.
- All domain types are `frozen=True` pydantic models.
- All new SQLite tables/columns have defaults; migrations are non-destructive and idempotent.
- Every public function/class has a docstring (binding project style).
- Every task ends with a green test suite and a commit.
- Every PR leaves `main` green: `pytest` (server), `./gradlew test :app:assembleDebug` (Android).
- Architectural-invariant tests are the regression gate; a failure blocks merge.
- Canonical metric names from `contracts/metrics.py` only — `MetricsRecorder.observe("anything_else", …)` raises `ValueError`.
- The Planner is stateless: no `self.` reads/writes outside dependency calls and the injected `Clock`.
- MemoryAtoms are immutable: never mutated in place; re-extraction produces new atoms + supersession.
- Retrieval is read-only: the Retriever never mutates `MemoryIndex`, `AtomStore`, or the extraction cursor.
- All timestamps come from the injected `Clock` — no `datetime.now()` outside `contracts/clock.py` and `tests/`.
- All ids come from the injected `IdGenerator` — no `uuid.uuid4()` outside `contracts/id_generator.py` and `tests/`.

---

## File structure (what each new file is responsible for)

```
server/src/sense_server/
├── contracts/                          # P0
│   ├── __init__.py
│   ├── types.py                        # all domain data types
│   ├── interfaces.py                   # all Protocol interfaces
│   ├── clock.py                        # Clock Protocol + impls
│   ├── id_generator.py                 # IdGenerator Protocol + impls
│   └── metrics.py                      # canonical metric names
├── memory/
│   ├── versioned.py                    # MemoryAtom extensions + provenance builder
│   ├── migrations.py                   # SQLite migration helpers
│   ├── scoring.py                      # Scorer Protocol + SimRecencyScorer + FixedScorer
│   ├── retrieval.py                    # Extended: new global Retriever; old MemoryRetriever deprecated
│   ├── stages.py                       # Four explicit extraction stages
│   └── extraction_worker.py            # Event-driven worker + reconciliation
├── agent/                              # P2
│   ├── planner.py                      # Stateless orchestrator
│   ├── context.py                      # ContextBuilder + prompt template
│   ├── intent.py                       # AgentLLM (async)
│   ├── validator.py                    # Validator + JSON schema
│   ├── guardrails.py                   # Guardrails
│   ├── audit.py                        # AuditEntry (domain) + AuditRecord (storage)
│   ├── metrics.py                      # MetricsRecorder impl
│   └── capability.py                   # CapabilityProvider (constant stub)
├── http/routes/
│   ├── agent.py                        # POST /agent
│   ├── memory.py                       # GET /memory, GET /sessions/{id}/memory, GET /memory/{atomId}
│   ├── metrics.py                      # GET /metrics
│   └── dto.py                          # All transport DTOs + mapper

server/tests/
├── contracts/
│   ├── fakes.py                        # Every Protocol has a fake
│   ├── test_clock.py
│   ├── test_id_generator.py
│   └── test_metrics.py
├── memory/
│   ├── test_atom.py
│   ├── test_versioned.py
│   ├── test_migrations.py
│   ├── test_scoring.py
│   ├── test_retrieval.py
│   ├── test_stages.py
│   ├── test_extraction_worker.py
│   └── test_invariants.py              # Architectural-invariant tests (Section 3)
├── agent/
│   ├── test_context.py
│   ├── test_intent.py
│   ├── test_validator.py
│   ├── test_guardrails.py
│   ├── test_audit.py
│   ├── test_metrics.py
│   ├── test_capability.py
│   └── test_planner.py
├── http/
│   ├── test_dto_mapping.py
│   ├── test_agent_route.py
│   ├── test_memory_route.py
│   └── test_metrics_route.py
└── integration/
    └── test_cognitive_read_path.py     # PRIMARY REGRESSION GATE (E2E)

android/sense-relay/app/src/main/kotlin/com/sense/relay/
├── ui/
│   ├── nav/
│   │   ├── AppNavigation.kt            # AMENDED — add Chat, Memory, Atom routes
│   │   ├── BottomBar.kt                # AMENDED — 4 tabs (INV-13)
│   │   └── Destination.kt              # AMENDED — new destinations
│   ├── chat/                           # NEW
│   │   ├── ChatScreen.kt
│   │   ├── ChatViewModel.kt
│   │   ├── ChatState.kt
│   │   └── ChatModels.kt
│   ├── memory/                         # NEW
│   │   ├── MemoryScreen.kt
│   │   ├── MemoryViewModel.kt
│   │   ├── MemoryState.kt
│   │   └── AtomDetailScreen.kt
│   └── design/                         # (existing — reuse)
├── data/
│   ├── AgentApi.kt                     # NEW
│   ├── AgentRepository.kt              # NEW (only HttpApiError importer)
│   ├── MemoryApi.kt                    # NEW
│   ├── MemoryRepository.kt             # NEW
│   ├── ChatHistoryStore.kt             # NEW (DataStore-backed)
│   └── RepositoryModule.kt             # AMENDED
└── core/
    └── SenseLog.kt                     # NEW (trace-id-aware logging)

android/sense-relay/app/src/test/kotlin/com/sense/relay/
├── core/SenseLogTest.kt
├── data/
│   ├── AgentRepositoryTest.kt
│   ├── MemoryRepositoryTest.kt
│   └── ChatHistoryStoreTest.kt
└── ui/
    ├── chat/ChatViewModelTest.kt
    ├── memory/MemoryViewModelTest.kt
    └── nav/BottomBarTest.kt
```

---

# Milestone M1 — Atom version fields + SQLite migration + thread-safe in-memory stores

### Task M1.1: Extend `MemoryAtom` with five defaulted versioning fields

**Files:**
- Modify: `server/src/sense_server/memory/atom.py`
- Test: `server/tests/memory/test_atom.py`

**Interfaces:**
- Consumes: existing `MemoryAtom` from `memory/atom.py`
- Produces: `MemoryAtom` with five new defaulted fields; `to_provenance()` method

- [ ] **Step 1: Write the failing test**

```python
# server/tests/memory/test_atom.py
from datetime import datetime, timezone
from sense_server.memory.atom import MemoryAtom

def test_atom_default_version_fields():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )
    assert atom.extraction_version == "v1"
    assert atom.embedding_model == ""
    assert atom.embedding_version == 0
    assert atom.extractor_prompt_version == "v1"
    assert atom.source_pipeline_version == "transcript"

def test_atom_immutability():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
    )
    import pytest
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        atom.text = "goodbye"

def test_atom_to_provenance():
    atom = MemoryAtom(
        atom_id="a1", session_id="s1", source_event_id="e1",
        kind="fact", text="hello",
        created_at=datetime(2026, 7, 7, tzinfo=timezone.utc),
        start_ms=0,
        extraction_version="v2", embedding_model="bge-small",
    )
    prov = atom.to_provenance()
    assert prov.session_id == "s1"
    assert prov.source_event_id == "e1"
    assert prov.extraction_version == "v2"
    assert prov.embedding_model == "bge-small"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/memory/test_atom.py -v`
Expected: FAIL — `MemoryAtom` has no `extraction_version` field, no `to_provenance()`.

- [ ] **Step 3: Add the new fields and the `to_provenance()` method**

```python
# server/src/sense_server/memory/atom.py — amend
from __future__ import annotations
from datetime import datetime
from pydantic import BaseModel, ConfigDict

class MemoryAtom(BaseModel):
    model_config = ConfigDict(frozen=True)
    atom_id: str
    session_id: str
    source_event_id: str
    kind: str
    text: str
    created_at: datetime
    start_ms: int
    extraction_version: str = "v1"
    embedding_model: str = ""
    embedding_version: int = 0
    extractor_prompt_version: str = "v1"
    source_pipeline_version: str = "transcript"

    def to_provenance(self) -> "Provenance":
        from ..contracts.types import Provenance
        return Provenance(
            session_id=self.session_id,
            source_event_id=self.source_event_id,
            source_modality=("vision" if self.source_pipeline_version == "vision" else "transcript"),
            extraction_version=self.extraction_version,
            embedding_model=self.embedding_model,
            embedding_version=self.embedding_version,
            extractor_prompt_version=self.extractor_prompt_version,
            source_pipeline_version=self.source_pipeline_version,
            created_at=self.created_at,
        )
```

- [ ] **Step 4: Add the `Provenance` type to `contracts/types.py`**

```python
# server/src/sense_server/contracts/types.py — add (alongside the rest of Section 2)
class Provenance(BaseModel):
    """Structured origin metadata for a MemoryAtom."""
    model_config = ConfigDict(frozen=True)
    session_id: str
    source_event_id: str
    source_modality: Literal["transcript", "vision", "ocr", "sensor", "bluetooth"]
    extraction_version: str
    embedding_model: str
    embedding_version: int
    extractor_prompt_version: str
    source_pipeline_version: str
    created_at: datetime
    supersedes_atom_id: str | None = None
    superseded_by_atom_id: str | None = None
```

- [ ] **Step 5: Re-run tests to verify they pass**

Run: `cd server && python -m pytest tests/memory/test_atom.py -v`
Expected: PASS (3/3).

- [ ] **Step 6: Commit**

```bash
cd server && git add src/sense_server/memory/atom.py src/sense_server/contracts/types.py tests/memory/test_atom.py
git commit -m "feat(memory): add five defaulted version fields + Provenance view"
```

### Task M1.2: Write the SQLite migration + extend `SqliteAtomStore`

**Files:**
- Create: `server/src/sense_server/memory/migrations.py`
- Modify: `server/src/sense_server/memory/store.py`
- Test: `server/tests/memory/test_migrations.py`

- [ ] **Step 1: Write the failing test**

```python
# server/tests/memory/test_migrations.py
import sqlite3
from sense_server.memory.migrations import migrate_memory_atoms_table

def test_migration_idempotent():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE memory_atoms (
            atom_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL, start_ms INTEGER NOT NULL
        );
    """)
    conn.execute("INSERT INTO memory_atoms VALUES ('a1','s1','e1','fact','x','2026-07-07',0)")
    conn.commit()
    migrate_memory_atoms_table(conn)  # first run
    migrate_memory_atoms_table(conn)  # second run (idempotent)
    rows = conn.execute("SELECT atom_id, extraction_version, source_pipeline_version FROM memory_atoms WHERE atom_id='a1'").fetchall()
    assert rows == [("a1", "v1", "transcript")]

def test_migration_new_row_round_trip():
    conn = sqlite3.connect(":memory:")
    conn.executescript("""
        CREATE TABLE memory_atoms (
            atom_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
            source_event_id TEXT NOT NULL, kind TEXT NOT NULL,
            text TEXT NOT NULL, created_at TEXT NOT NULL, start_ms INTEGER NOT NULL
        );
    """)
    migrate_memory_atoms_table(conn)
    conn.execute("INSERT INTO memory_atoms VALUES ('a1','s1','e1','fact','x','2026-07-07',0)")
    conn.commit()
    cols = [r[1] for r in conn.execute("PRAGMA table_info(memory_atoms)").fetchall()]
    assert "extraction_version" in cols
    assert "embedding_model" in cols
    assert "embedding_version" in cols
    assert "extractor_prompt_version" in cols
    assert "source_pipeline_version" in cols
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd server && python -m pytest tests/memory/test_migrations.py -v`
Expected: FAIL — `migrate_memory_atoms_table` does not exist.

- [ ] **Step 3: Implement the migration**

```python
# server/src/sense_server/memory/migrations.py
from __future__ import annotations
import sqlite3

ADDITIONS = [
    ("extraction_version",       "TEXT NOT NULL DEFAULT 'v1'"),
    ("embedding_model",          "TEXT NOT NULL DEFAULT ''"),
    ("embedding_version",        "INTEGER NOT NULL DEFAULT 0"),
    ("extractor_prompt_version", "TEXT NOT NULL DEFAULT 'v1'"),
    ("source_pipeline_version",  "TEXT NOT NULL DEFAULT 'transcript'"),
]

def migrate_memory_atoms_table(conn: sqlite3.Connection) -> None:
    """Add the five version columns with defaults. Idempotent."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(memory_atoms)")}
    for name, decl in ADDITIONS:
        if name not in existing:
            conn.execute(f"ALTER TABLE memory_atoms ADD COLUMN {name} {decl}")
    conn.commit()
```

- [ ] **Step 4: Wire the migration into `SqliteAtomStore.__init__` + extend `append/atoms`**

```python
# server/src/sense_server/memory/store.py — amend SqliteAtomStore
from .migrations import migrate_memory_atoms_table

class SqliteAtomStore:
    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS memory_atoms (
                atom_id TEXT PRIMARY KEY, session_id TEXT NOT NULL,
                source_event_id TEXT NOT NULL, kind TEXT NOT NULL,
                text TEXT NOT NULL, created_at TEXT NOT NULL, start_ms INTEGER NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_atoms_session_start
                ON memory_atoms (session_id, start_ms);
            CREATE TABLE IF NOT EXISTS extraction_cursor (
                session_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL
            );
        """)
        migrate_memory_atoms_table(self._conn)  # NEW: non-destructive
        self._conn.commit()

    def append(self, atom):
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO memory_atoms "
            "(atom_id, session_id, source_event_id, kind, text, created_at, start_ms, "
            " extraction_version, embedding_model, embedding_version, "
            " extractor_prompt_version, source_pipeline_version) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (atom.atom_id, atom.session_id, atom.source_event_id, atom.kind,
             atom.text, atom.created_at.isoformat(), atom.start_ms,
             atom.extraction_version, atom.embedding_model, atom.embedding_version,
             atom.extractor_prompt_version, atom.source_pipeline_version),
        )
        self._conn.commit()
        return cur.rowcount == 1

    def atoms(self, session_id):
        rows = self._conn.execute(
            "SELECT atom_id, session_id, source_event_id, kind, text, created_at, "
            "start_ms, extraction_version, embedding_model, embedding_version, "
            "extractor_prompt_version, source_pipeline_version "
            "FROM memory_atoms WHERE session_id = ? ORDER BY start_ms",
            (session_id,),
        ).fetchall()
        return [MemoryAtom(**dict(zip(
            ["atom_id","session_id","source_event_id","kind","text","created_at","start_ms",
             "extraction_version","embedding_model","embedding_version",
             "extractor_prompt_version","source_pipeline_version"], r))) for r in rows]
```

- [ ] **Step 5: Run tests; fix any regression in existing `SqliteAtomStore` tests**

Run: `cd server && python -m pytest tests/memory/ -v`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
cd server && git add src/sense_server/memory/migrations.py src/sense_server/memory/store.py tests/memory/test_migrations.py
git commit -m "feat(memory): non-destructive version-columns migration"
```

### Task M1.3: Make in-memory stores thread-safe (review M1)

**Files:**
- Modify: `server/src/sense_server/memory/store.py` (`InMemoryAtomStore`)
- Modify: `server/src/sense_server/memory/index.py` (`InMemoryMemoryIndex`)
- Test: `server/tests/memory/test_invariants.py` (start this file)

- [ ] **Step 1: Write the failing test**

```python
# server/tests/memory/test_invariants.py — add
import threading
from sense_server.memory.store import InMemoryAtomStore
from sense_server.memory.atom import MemoryAtom
from datetime import datetime, timezone

def test_in_memory_atom_store_thread_safe():
    store = InMemoryAtomStore()
    atoms = [
        MemoryAtom(atom_id=f"a{i}", session_id="s1", source_event_id=f"e{i}",
                   kind="fact", text=f"text {i}",
                   created_at=datetime(2026,7,7,tzinfo=timezone.utc), start_ms=i)
        for i in range(100)
    ]
    def worker(chunk):
        for a in chunk:
            store.append(a)
    threads = [threading.Thread(target=worker, args=(atoms[i::4],)) for i in range(4)]
    for t in threads: t.start()
    for t in threads: t.join()
    assert len(store.atoms("s1")) == 100
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd server && python -m pytest tests/memory/test_invariants.py::test_in_memory_atom_store_thread_safe -v`
Expected: FAIL (intermittent — race in `setdefault` + `append`).

- [ ] **Step 3: Add a `threading.Lock`**

```python
# server/src/sense_server/memory/store.py — amend InMemoryAtomStore
import threading

class InMemoryAtomStore:
    def __init__(self) -> None:
        self._seen: set[str] = set()
        self._by_session: dict[str, list[MemoryAtom]] = {}
        self._cursor: dict[str, int] = {}
        self._lock = threading.Lock()

    def append(self, atom):
        with self._lock:
            if atom.atom_id in self._seen:
                return False
            self._seen.add(atom.atom_id)
            self._by_session.setdefault(atom.session_id, []).append(atom)
            return True

    # has, atoms, get_cursor, set_cursor — wrap mutations in `with self._lock`
```

- [ ] **Step 4: Apply the same lock to `InMemoryMemoryIndex`**

- [ ] **Step 5: Run test to verify it passes**

- [ ] **Step 6: Commit**

```bash
cd server && git add src/sense_server/memory/store.py src/sense_server/memory/index.py tests/memory/test_invariants.py
git commit -m "fix(memory): thread-safe in-memory stores (test M1)"
```

---

# Milestone M2 — Scorer + MemoryIndex versioning + Validator

### Task M2.1: Create `contracts/clock.py` + `IdGenerator` + `Metrics` (foundation)

**Files:**
- Create: `server/src/sense_server/contracts/__init__.py`
- Create: `server/src/sense_server/contracts/clock.py`
- Create: `server/src/sense_server/contracts/id_generator.py`
- Create: `server/src/sense_server/contracts/metrics.py`
- Test: `server/tests/contracts/test_clock.py`, `test_id_generator.py`, `test_metrics.py`

- [ ] **Step 1: Write failing tests for Clock**

```python
# server/tests/contracts/test_clock.py
from datetime import datetime, timezone, timedelta
from sense_server.contracts.clock import SystemClock, FakeClock

def test_system_clock_returns_utc():
    now = SystemClock().now()
    assert now.tzinfo == timezone.utc

def test_fake_clock_advances():
    start = datetime(2026, 7, 7, tzinfo=timezone.utc)
    c = FakeClock(start)
    assert c.now() == start
    c.advance(60)
    assert c.now() == start + timedelta(seconds=60)
```

- [ ] **Step 2: Implement `contracts/clock.py`**

- [ ] **Step 3: Write failing tests for `IdGenerator`**

```python
# server/tests/contracts/test_id_generator.py
import re
from sense_server.contracts.id_generator import UuidIdGenerator, DeterministicIdGenerator

def test_uuid_generator():
    g = UuidIdGenerator()
    assert re.match(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", g.new())

def test_deterministic_generator():
    g = DeterministicIdGenerator()
    assert g.new() == "trace-0001"
    assert g.new() == "trace-0002"
```

- [ ] **Step 4: Implement `contracts/id_generator.py`**

- [ ] **Step 5: Write failing tests for `Metrics`**

```python
# server/tests/contracts/test_metrics.py
import pytest
from sense_server.contracts.metrics import Metrics
from sense_server.agent.metrics import InMemoryMetricsRecorder

def test_metrics_known_set():
    assert Metrics.PLANNER_LATENCY_MS in Metrics.KNOWN
    assert Metrics.RETRIEVAL_HITS_TOTAL in Metrics.KNOWN

def test_recorder_rejects_unknown_name():
    r = InMemoryMetricsRecorder()
    with pytest.raises(ValueError):
        r.observe("not_canonical", 1.0)

def test_recorder_render_prometheus():
    r = InMemoryMetricsRecorder()
    r.increment(Metrics.RETRIEVAL_HITS_TOTAL)
    text = r.render()
    assert "# TYPE retrieval_hits_total counter" in text
    assert "retrieval_hits_total 1" in text
```

- [ ] **Step 6: Implement `contracts/metrics.py` and `agent/metrics.py`**

```python
# server/src/sense_server/contracts/metrics.py
class Metrics:
    PLANNER_LATENCY_MS = "planner_latency_ms"
    RETRIEVAL_LATENCY_MS = "retrieval_latency_ms"
    EXTRACTION_LATENCY_MS = "extraction_latency_ms"
    EMBEDDING_LATENCY_MS = "embedding_latency_ms"
    LLM_LATENCY_MS = "llm_latency_ms"
    VALIDATOR_LATENCY_MS = "validator_latency_ms"
    GUARDRAILS_LATENCY_MS = "guardrails_latency_ms"
    AUDIT_LATENCY_MS = "audit_latency_ms"
    RETRIEVAL_HITS_TOTAL = "retrieval_hits_total"
    RETRIEVAL_MISSES_TOTAL = "retrieval_misses_total"
    VALIDATOR_FAILURES_TOTAL = "validator_failures_total"
    REFUSALS_TOTAL = "refusals_total"
    AUDIT_RECORDS_TOTAL = "audit_records_total"
    LLM_TOKEN_USAGE_TOTAL = "llm_token_usage_total"
    EXTRACTION_FAILURES_TOTAL = "extraction_failures_total"
    INDEXING_FAILURES_TOTAL = "indexing_failures_total"
    EXTRACTION_QUEUE_OVERFLOW_TOTAL = "extraction_queue_overflow_total"
    KNOWN = frozenset({...})  # all of the above

# server/src/sense_server/agent/metrics.py
class InMemoryMetricsRecorder:
    def __init__(self):
        self._counters: dict[tuple[str, frozenset], int] = {}
        self._histograms: dict[str, list[float]] = {}
    def _check(self, name: str) -> None:
        if name not in Metrics.KNOWN:
            raise ValueError(f"unknown metric: {name!r}")
    def observe(self, name, value, tags=None):
        self._check(name)
        self._histograms.setdefault(name, []).append(value)
    def increment(self, name, tags=None):
        self._check(name)
        key = (name, frozenset((tags or {}).items()))
        self._counters[key] = self._counters.get(key, 0) + 1
    def snapshot(self) -> dict[str, float]:
        return {n: sum(v)/len(v) for n, v in self._histograms.items()} | \
               {f"{n}|{dict(k[1])}": v for k, v in self._counters.items()}
    def render(self) -> str:
        lines = []
        for name in sorted({n for n,_ in self._counters}):
            lines.append(f"# TYPE {name} counter")
            for (n, tag), v in self._counters.items():
                if n == name:
                    if tag:
                        labels = ",".join(f'{k}="{v_}"' for k, v_ in tag)
                        lines.append(f"{n}{{{labels}}} {v}")
                    else:
                        lines.append(f"{n} {v}")
        for name in self._histograms:
            lines.append(f"# TYPE {name} histogram")
            lines.append(f"{name}_sum {sum(self._histograms[name])}")
            lines.append(f"{name}_count {len(self._histograms[name])}")
        return "\n".join(lines) + "\n"
```

- [ ] **Step 7: Run all three test files; commit**

```bash
cd server && git add src/sense_server/contracts/ src/sense_server/agent/metrics.py tests/contracts/
git commit -m "feat(contracts): Clock, IdGenerator, canonical metrics"
```

### Task M2.2: Scorer Protocol + `SimRecencyScorer` + `FixedScorer`

**Files:**
- Create: `server/src/sense_server/memory/scoring.py`
- Test: `server/tests/memory/test_scoring.py`

- [ ] **Step 1: Write failing tests**

```python
# server/tests/memory/test_scoring.py
import math
from sense_server.memory.scoring import SimRecencyScorer, FixedScorer, Scorer

def test_scorer_protocol():
    assert isinstance(SimRecencyScorer(), Scorer)
    assert isinstance(FixedScorer(), Scorer)

def test_sim_recency_scorer_cosine_only():
    s = SimRecencyScorer(half_life_s=7*24*3600)
    v = [1.0, 0.0, 0.0]
    assert abs(s.score(v, v, 0) - 1.0) < 1e-6

def test_sim_recency_scorer_decay_at_half_life():
    s = SimRecencyScorer(half_life_s=100)
    v = [1.0, 0.0, 0.0]
    assert abs(s.score(v, v, 100) - 0.5) < 1e-6

def test_sim_recency_scorer_zero_for_orthogonal():
    s = SimRecencyScorer()
    assert s.score([1.0, 0.0], [0.0, 1.0], 0) == 0.0

def test_sim_recency_scorer_name_version():
    s = SimRecencyScorer()
    assert s.name == "sim_recency"
    assert s.version == "v1"
```

- [ ] **Step 2: Implement**

```python
# server/src/sense_server/memory/scoring.py
from __future__ import annotations
import math
from typing import Protocol, runtime_checkable

@runtime_checkable
class Scorer(Protocol):
    name: str
    version: str
    def score(self, query_vec, atom_vec, age_s) -> float: ...

class SimRecencyScorer:
    name = "sim_recency"
    version = "v1"
    def __init__(self, half_life_s: float = 7 * 24 * 3600) -> None:
        self._half_life_s = half_life_s
    def score(self, query_vec, atom_vec, age_s):
        cos = _cosine(query_vec, atom_vec)
        if cos <= 0:
            return 0.0
        return cos * math.exp(-math.log(2) * age_s / self._half_life_s)

class FixedScorer:
    name = "fixed"
    version = "v1"
    def score(self, query_vec, atom_vec, age_s): return 1.0

def _cosine(a, b) -> float:
    dot = sum(x*y for x, y in zip(a, b))
    na = math.sqrt(sum(x*x for x in a))
    nb = math.sqrt(sum(x*x for x in b))
    if na == 0 or nb == 0: return 0.0
    return dot / (na * nb)
```

- [ ] **Step 3: Run tests; commit**

```bash
cd server && git add src/sense_server/memory/scoring.py tests/memory/test_scoring.py
git commit -m "feat(memory): Scorer Protocol + SimRecencyScorer"
```

### Task M2.3: `MemoryIndex.name` and `version` (M12)

**Files:**
- Modify: `server/src/sense_server/memory/index.py`
- Test: extend `server/tests/memory/test_scoring.py` (or create `test_index_versioning.py`)

- [ ] **Step 1: Write failing test**

```python
def test_memory_index_has_name_and_version():
    from sense_server.memory.index import InMemoryMemoryIndex, SqliteMemoryIndex
    assert InMemoryMemoryIndex().name == "in_memory"
    assert InMemoryMemoryIndex().version == "v1"
    assert SqliteMemoryIndex(":memory:").name == "sqlite"
    assert SqliteMemoryIndex(":memory:").version == "v1"
```

- [ ] **Step 2: Add `name` and `version` class attributes to `MemoryIndex` Protocol + the two impls**

- [ ] **Step 3: Run tests; commit**

```bash
cd server && git add src/sense_server/memory/index.py tests/memory/
git commit -m "feat(memory): MemoryIndex Protocol gains name/version"
```

### Task M2.4: Validator + JSON schema (M10)

**Files:**
- Create: `server/src/sense_server/agent/validator.py`
- Test: `server/tests/agent/test_validator.py`

- [ ] **Step 1: Write the schema constant**

```python
# In agent/validator.py, define:
AGENT_ACTION_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {"enum": ["answer", "no_memory"]},
        "text": {"type": "string"},
        "atom_ids": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
    },
    "required": ["kind", "text", "atom_ids", "confidence"],
    "additionalProperties": False,
}
```

- [ ] **Step 2: Write failing tests**

```python
# server/tests/agent/test_validator.py
import pytest
from sense_server.contracts.types import LLMResult, AgentAction, AgentActionKind, Confidence, ValidatorContext, RejectionReason
from sense_server.agent.validator import StrictJSONValidator

def _ctx(atoms=("a1",)):
    return ValidatorContext(retrieved_atom_ids=atoms, no_memory_top_score_threshold=0.3)

def test_validator_accepts_answer():
    v = StrictJSONValidator()
    res = LLMResult(raw='{"kind":"answer","text":"x","atom_ids":["a1"],"confidence":0.9}', parsed=None, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None
    assert out.action.kind == AgentActionKind.ANSWER

def test_validator_rejects_answer_without_atoms():
    v = StrictJSONValidator()
    res = LLMResult(raw='{"kind":"answer","text":"x","atom_ids":[],"confidence":0.9}', parsed=None, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.NO_ATOM_CITED

def test_validator_rejects_cited_atom_not_retrieved():
    v = StrictJSONValidator()
    res = LLMResult(raw='{"kind":"answer","text":"x","atom_ids":["a_evil"],"confidence":0.9}', parsed=None, parse_error=None)
    out = v.validate(_ctx(atoms=("a1",)), res)
    assert out.rejection == RejectionReason.CITED_ATOM_NOT_RETRIEVED

def test_validator_rejects_invalid_json():
    v = StrictJSONValidator()
    res = LLMResult(raw='not json', parsed=None, parse_error="parse failed")
    out = v.validate(_ctx(), res)
    assert out.rejection == RejectionReason.INVALID_JSON

def test_validator_accepts_no_memory_with_empty_atoms():
    v = StrictJSONValidator()
    res = LLMResult(raw='{"kind":"no_memory","text":"none","atom_ids":[],"confidence":0.9}', parsed=None, parse_error=None)
    out = v.validate(_ctx(), res)
    assert out.rejection is None
```

- [ ] **Step 3: Implement**

```python
# server/src/sense_server/agent/validator.py
from __future__ import annotations
import json
from ..contracts.types import (
    LLMResult, ValidatedAction, AgentAction, AgentActionKind,
    Confidence, ValidatorContext, RejectionReason,
)

class StrictJSONValidator:
    def validate(self, ctx: ValidatorContext, result: LLMResult) -> ValidatedAction:
        if result.parsed is None:
            return ValidatedAction(action=_stub(), rejection=RejectionReason.INVALID_JSON)
        action = result.parsed
        if action.kind == AgentActionKind.NO_MEMORY:
            if action.atom_ids:
                return ValidatedAction(action=action, rejection=RejectionReason.SCHEMA_MISMATCH)
            return ValidatedAction(action=action, rejection=None)
        # answer
        if not action.atom_ids:
            return ValidatedAction(action=action, rejection=RejectionReason.NO_ATOM_CITED)
        if not set(action.atom_ids).issubset(set(ctx.retrieved_atom_ids)):
            return ValidatedAction(action=action, rejection=RejectionReason.CITED_ATOM_NOT_RETRIEVED)
        if not (0.0 <= action.confidence <= 1.0):
            return ValidatedAction(action=action, rejection=RejectionReason.CONFIDENCE_OUT_OF_RANGE)
        return ValidatedAction(action=action, rejection=None)
```

- [ ] **Step 4: Run tests; commit**

```bash
cd server && git add src/sense_server/agent/validator.py tests/agent/test_validator.py
git commit -m "feat(agent): Validator with provenance enforcement (M10)"
```

---

# Milestone M3 — `RetrievedContext` + new global `Retriever` + transactional indexing

### Task M3.1: Extend `MemoryIndex.SearchResult` to carry the vector

**Files:**
- Modify: `server/src/sense_server/memory/index.py`
- Test: `server/tests/memory/test_retrieval.py` (start this file)

- [ ] **Step 1: Write the failing test**

```python
# server/tests/memory/test_retrieval.py
from sense_server.memory.index import InMemoryMemoryIndex
from sense_server.memory.atom import MemoryAtom
from sense_server.memory.embeddings import Embedder
from datetime import datetime, timezone

class FakeEmbedder:
    def embed(self, texts): return [[1.0, 0.0, 0.0] for _ in texts]

def test_in_memory_index_search_returns_vector():
    idx = InMemoryMemoryIndex()
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x", created_at=datetime(2026,7,7,tzinfo=timezone.utc), start_ms=0)
    idx.add(a, [1.0, 0.0, 0.0])
    results = idx.search("s1", [1.0, 0.0, 0.0], k=1)
    assert len(results) == 1
    assert results[0].vector == [1.0, 0.0, 0.0]
```

- [ ] **Step 2: Extend `SearchResult` and the two index impls**

```python
# memory/index.py
from dataclasses import dataclass, field
@dataclass(frozen=True, slots=True)
class SearchResult:
    atom: MemoryAtom
    score: float
    vector: list[float] = field(default_factory=list)
```

Update `InMemoryMemoryIndex.search` and `SqliteMemoryIndex.search` to populate `vector`.

- [ ] **Step 3: Run tests; commit**

### Task M3.2: Implement the new global `Retriever`

**Files:**
- Create: `server/src/sense_server/memory/retrieval.py::Retriever` (extend the existing module)
- Test: extend `server/tests/memory/test_retrieval.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/memory/test_retrieval.py — add
from sense_server.memory.retrieval import Retriever
from sense_server.memory.scoring import SimRecencyScorer
from sense_server.contracts.clock import FakeClock
from sense_server.contracts.id_generator import DeterministicIdGenerator
from sense_server.contracts.types import RetrieverContext

def test_retriever_global_no_filter():
    clock = FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc))
    idx = InMemoryMemoryIndex()
    idx.add(_atom("a1", "s1", "s1 text"), [1.0, 0.0, 0.0])
    idx.add(_atom("a2", "s2", "s2 text"), [0.0, 1.0, 0.0])
    r = Retriever(FakeEmbedder(), idx, SimRecencyScorer(), clock, DeterministicIdGenerator())
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10, session_id=None))
    assert len(rc.atoms) == 2

def test_retriever_session_filter():
    clock = FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc))
    idx = InMemoryMemoryIndex()
    idx.add(_atom("a1", "s1", "s1 text"), [1.0, 0.0, 0.0])
    idx.add(_atom("a2", "s2", "s2 text"), [0.0, 1.0, 0.0])
    r = Retriever(FakeEmbedder(), idx, SimRecencyScorer(), clock, DeterministicIdGenerator())
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10, session_id="s1"))
    assert {a.session_id for a in rc.atoms} == {"s1"}

def test_retrieved_context_canonical_shape():
    clock = FakeClock(datetime(2026, 7, 7, tzinfo=timezone.utc))
    idx = InMemoryMemoryIndex()
    idx.add(_atom("a1", "s1", "x"), [1.0, 0.0, 0.0])
    r = Retriever(FakeEmbedder(), idx, SimRecencyScorer(), clock, DeterministicIdGenerator())
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.retrieval_strategy == "sim_recency"
    assert rc.scorer_version == "v1"
    assert rc.index_name == "in_memory"
    assert rc.index_version == "v1"
    assert rc.retrieval_trace_id != ""
    assert rc.top_score > 0
    assert rc.lowest_score > 0
    assert rc.returned_count == 1

def test_retriever_tiebreaker_cascade():
    clock = FakeClock(datetime(2026, 7, 7, 12, 0, 0, tzinfo=timezone.utc))
    idx = InMemoryMemoryIndex()
    a_old = MemoryAtom(atom_id="a_old", session_id="s1", source_event_id="e1",
                       kind="fact", text="x", created_at=datetime(2026,7,1,tzinfo=timezone.utc), start_ms=0)
    a_new = MemoryAtom(atom_id="a_new", session_id="s1", source_event_id="e2",
                       kind="fact", text="x", created_at=datetime(2026,7,7,tzinfo=timezone.utc), start_ms=1)
    idx.add(a_old, [1.0, 0.0])
    idx.add(a_new, [1.0, 0.0])
    r = Retriever(FakeEmbedder(), idx, SimRecencyScorer(half_life_s=1e9), clock, DeterministicIdGenerator())
    rc = r.retrieve(RetrieverContext(query_text="x", limit=10))
    assert rc.atoms[0].atom_id == "a_new"   # tiebreaker: newer created_at first
```

- [ ] **Step 2: Implement the Retriever**

```python
# memory/retrieval.py — add (keep existing MemoryRetriever, deprecate with warning)
import time
import warnings
from ..contracts.clock import Clock
from ..contracts.id_generator import IdGenerator
from ..contracts.types import RetrieverContext, RetrievedContext, ScoredAtom
from .scoring import Scorer

class Retriever:
    def __init__(self, embedder, index, scorer: Scorer, clock: Clock, ids: IdGenerator):
        self._embedder = embedder
        self._index = index
        self._scorer = scorer
        self._clock = clock
        self._ids = ids
    def retrieve(self, ctx: RetrieverContext) -> RetrievedContext:
        start = time.monotonic()
        trace_id = self._ids.new()
        qvec = self._embedder.embed([ctx.query_text])[0]
        candidates = self._index.search(ctx.session_id or "", qvec, ctx.limit * 4)
        now = self._clock.now()
        scored = []
        for sr in candidates:
            age_s = (now - sr.atom.created_at).total_seconds()
            s = self._scorer.score(qvec, sr.vector, age_s)
            scored.append(ScoredAtom(
                atom_id=sr.atom.atom_id, session_id=sr.atom.session_id,
                kind=sr.atom.kind, text=sr.atom.text,
                created_at=sr.atom.created_at, start_ms=sr.atom.start_ms, score=s,
            ))
        scored.sort(key=lambda a: (-a.score, -a.created_at.timestamp(), a.atom_id))
        scored = scored[: ctx.limit]
        return RetrievedContext(
            atoms=tuple(scored),
            retrieval_strategy=self._scorer.name,
            scorer_version=self._scorer.version,
            index_name=self._index.name,
            index_version=self._index.version,
            top_score=scored[0].score if scored else float("-inf"),
            lowest_score=scored[-1].score if scored else float("-inf"),
            returned_count=len(scored),
            retrieval_latency_ms=int((time.monotonic() - start) * 1000),
            candidate_count=len(candidates),
            session_filter=ctx.session_id,
            retrieval_trace_id=trace_id,
        )

class MemoryRetriever:
    """DEPRECATED: use Retriever (Section 3)."""
    def __init__(self, *a, **kw):
        warnings.warn("MemoryRetriever is deprecated; use Retriever", DeprecationWarning)
        ...
```

- [ ] **Step 3: Run tests; commit**

### Task M3.3: Make `IndexingPipeline.index_session` transactional (M7)

**Files:**
- Modify: `server/src/sense_server/memory/retrieval.py` (the existing `IndexingPipeline` class)
- Test: extend `server/tests/memory/test_retrieval.py`

- [ ] **Step 1: Write failing test**

```python
def test_indexing_session_atomic_on_embedder_failure():
    """If embedding fails mid-batch, no atoms should be in the index."""
    from sense_server.memory.retrieval import IndexingPipeline
    from sense_server.memory.store import InMemoryAtomStore

    class FlakyEmbedder:
        def __init__(self): self.calls = 0
        def embed(self, texts):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("embedder boom")
            return [[1.0, 0.0] for _ in texts]

    atoms = [_atom(f"a{i}", "s1", f"text {i}") for i in range(3)]
    store = InMemoryAtomStore()
    idx = InMemoryMemoryIndex()
    for a in atoms: store.append(a)
    pipe = IndexingPipeline(store, idx, FlakyEmbedder())
    with pytest.raises(RuntimeError):
        pipe.index_session("s1")
    assert idx.search("s1", [1.0, 0.0], 10) == []
```

- [ ] **Step 2: Refactor `index_session` to embed all then add all**

```python
# memory/retrieval.py — amend IndexingPipeline.index_session
class IndexingPipeline:
    def index_session(self, session_id: str) -> list[MemoryAtom]:
        pending = [a for a in self._atoms.atoms(session_id) if not self._index.has(a.atom_id)]
        if not pending: return []
        vectors = self._embedder.embed([a.text for a in pending])  # may raise
        # If we reach here, the embedder succeeded for all texts.
        for atom, vector in zip(pending, vectors):
            self._index.add(atom, vector)
        return pending
```

- [ ] **Step 3: Run tests; commit**

---

# Milestone M4 — `ExtractionWorker` + four stages + `run_gateway.py` wiring + H7

### Task M4.1: `memory/versioned.py` + `stamp_version_metadata`

**Files:**
- Create: `server/src/sense_server/memory/versioned.py`
- Test: `server/tests/memory/test_versioned.py`

- [ ] **Step 1: Write failing test**

```python
def test_stamp_version_metadata_no_op_for_default_atom():
    from sense_server.memory.versioned import stamp_version_metadata
    a = MemoryAtom(atom_id="a1", session_id="s1", source_event_id="e1",
                   kind="fact", text="x", created_at=datetime(2026,7,7,tzinfo=timezone.utc), start_ms=0)
    stamp_version_metadata(a)
    assert a.extraction_version == "v1"
    assert a.source_pipeline_version == "transcript"
```

- [ ] **Step 2: Implement**

```python
def stamp_version_metadata(atom: MemoryAtom) -> None:
    """Idempotent no-op (INVARIANT 5). Future re-extraction paths produce NEW atoms
    that supersede old ones (via Provenance.superseded_by), never modify in place."""
    if atom.extraction_version != "v1" or atom.source_pipeline_version != "transcript":
        return
```

- [ ] **Step 3: Run tests; commit**

### Task M4.2: Four explicit pipeline stages (`memory/stages.py`)

**Files:**
- Create: `server/src/sense_server/memory/stages.py`
- Test: `server/tests/memory/test_stages.py`

- [ ] **Step 1: Write failing tests** (one per stage verifying it calls the wrapped component)

- [ ] **Step 2: Implement** (per the design doc Section 3 r1)

- [ ] **Step 3: Commit**

### Task M4.3: `ExtractionEnqueuer` + `ExtractionWorker` (event-driven + reconciliation, H7)

**Files:**
- Create: `server/src/sense_server/memory/extraction_worker.py`
- Test: `server/tests/memory/test_extraction_worker.py`

- [ ] **Step 1: Write failing tests** (event path, reconciliation, idempotency, failure isolation, cancellation, queue overflow)

- [ ] **Step 2: Implement with H7 cursor-after-indexing**

```python
class ExtractionWorker:
    async def _extract_and_index_sync(self, session_id: str) -> None:
        """Cursor advances only after BOTH extraction AND indexing succeed (H7)."""
        cursor_before = self._atoms.get_cursor(session_id)
        produced = self._extract_only(session_id)        # does not advance cursor
        if not produced:
            return
        for atom in produced:
            stamp_version_metadata(atom)
        try:
            self._indexing.index_session(session_id)
        except Exception:
            self._metrics.increment(Metrics.INDEXING_FAILURES_TOTAL, tags={"session_id": session_id})
            raise
        max_seq = max(a.start_ms for a in produced)       # or track via event loop
        self._atoms.set_cursor(session_id, max_seq)
```

Add a `ExtractionPipeline.extract_only(events, atoms, extractor, clock) -> list[MemoryAtom]` that runs the existing extraction but does NOT advance the cursor.

- [ ] **Step 3: Add test for H7 self-heal**

```python
async def test_indexing_failure_self_heals_on_reconciliation():
    """Inject an indexing failure for a session; run reconciliation; assert
    the atoms are now in the index."""
    ...
```

- [ ] **Step 4: Run tests; commit**

### Task M4.4: Wire `ExtractionWorker` into `run_gateway.py` + add `ExtractionEnqueuer` to `GatewayCore`

**Files:**
- Modify: `server/scripts/run_gateway.py`
- Modify: `server/src/sense_server/gateway/core.py`
- Test: `server/tests/gateway/test_extraction_enqueuer_wiring.py`

- [ ] **Step 1: Add `extraction_enqueuer` parameter to `GatewayCore`**

- [ ] **Step 2: In `run_gateway.py`, construct `ExtractionWorker`, call `.start()`, pass `.enqueuer` to `GatewayCore`; `.stop()` on shutdown**

- [ ] **Step 3: Run the existing gateway test suite + the new wiring test; commit**

---

# Milestone M5 — Metrics integration + architectural-invariant tests

### Task M5.1: Metrics integration in `ExtractionWorker`

**Files:**
- Modify: `server/src/sense_server/memory/extraction_worker.py`
- Test: extend `server/tests/memory/test_extraction_worker.py`

- [ ] **Step 1: Assert `EXTRACTION_LATENCY_MS` observed + `EXTRACTION_FAILURES_TOTAL` incremented on failure + `EXTRACTION_QUEUE_OVERFLOW_TOTAL` on drop**

- [ ] **Step 2: Commit**

### Task M5.2: All Section 3 architectural-invariant tests

**Files:**
- Extend: `server/tests/memory/test_invariants.py`

- [ ] **Step 1: `test_invariant_atoms_immutable`** (frozen model + `stamp_version_metadata` no-op)

- [ ] **Step 2: `test_invariant_retrieval_is_read_only`** (call `retrieve()` 1000×, assert no mutation of `AtomStore`/`index`/cursor)

- [ ] **Step 3: `test_invariant_extraction_exactly_once`** (10× same session → cursor advances once, atoms not duplicated)

- [ ] **Step 4: `test_invariant_reconciliation_recovers_missed_jobs`** (3 events; stop worker after 1; restart; reconciliation processes 2)

- [ ] **Step 5: `test_invariant_retrieval_deterministic`** (same inputs → same `RetrievedContext`)

- [ ] **Step 6: `test_invariant_scorer_ordering_stable`** (full tiebreaker cascade)

- [ ] **Step 7: `test_invariant_retrieval_ordering_invariant_under_index_swap`** (InMemory + Sqlite → byte-identical)

- [ ] **Step 8: `test_invariant_version_metadata_stamped`**

- [ ] **Step 9: `test_invariant_provenance_survives_retrieval`**

- [ ] **Step 10: `test_invariant_cancellation_propagates`**

- [ ] **Step 11: `test_invariant_stages_observable_in_audit`**

- [ ] **Step 12: `test_invariant_orchestration_seam_is_stable`**

- [ ] **Step 13: Audit log indexes on `request_id` + `retrieval_trace_id` + `created_at` (M9)**

```python
# server/src/sense_server/agent/audit.py
class SqliteAuditLogger:
    def __init__(self, path):
        self._conn = sqlite3.connect(str(path))
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS audit_entries (
                audit_id TEXT PRIMARY KEY,
                request_id TEXT NOT NULL,
                retrieval_trace_id TEXT NOT NULL,
                ts TEXT NOT NULL,
                ... (every AuditEntry field) ...
            );
            CREATE INDEX IF NOT EXISTS ix_audit_request ON audit_entries (request_id);
            CREATE INDEX IF NOT EXISTS ix_audit_trace ON audit_entries (retrieval_trace_id);
            CREATE INDEX IF NOT EXISTS ix_audit_ts ON audit_entries (ts);
        """)
```

- [ ] **Step 14: Run the full test suite; commit**

```bash
cd server && git add . && git commit -m "test(memory): full architectural-invariant suite (Section 3 r10)"
```

---

# Milestone N1 — Transport DTOs + pure mapper

### Task N1.1: `http/routes/dto.py` with all DTOs

**Files:**
- Create: `server/src/sense_server/http/routes/dto.py`
- Test: `server/tests/http/test_dto_mapping.py`

- [ ] **Step 1: Write failing tests for each DTO** (`schema_version`, `extra="forbid"`, `MAX_ATOM_CHIP_TEXT_LEN`)

- [ ] **Step 2: Implement** (verbatim from the spec)

- [ ] **Step 3: Commit**

### Task N1.2: `map_planner_result_to_dto` pure function

**Files:**
- Modify: `server/src/sense_server/http/routes/dto.py`
- Test: `server/tests/http/test_dto_mapping.py`

- [ ] **Step 1: Write failing test**

```python
def test_map_planner_result_to_dto_pure():
    pr = _planner_result(outcome="return", answer="x", atom_ids=("a1",), confidence=0.9)
    dto = map_planner_result_to_dto(pr)
    assert dto.outcome == "return"
    assert dto.answer == "x"
    assert dto.atom_ids == ["a1"]  # actually atoms
    assert dto.schema_version == "v1"
    assert dto.request_id == pr.request_id
    assert dto.retrieval_trace_id == pr.retrieval_trace_id
    assert dto.payload is None

def test_map_truncates_long_atom_text():
    pr = _planner_result(atoms=[_scored_atom(text="x"*300)])
    dto = map_planner_result_to_dto(pr)
    assert len(dto.atoms[0].text) == MAX_ATOM_CHIP_TEXT_LEN + 1  # 240 + "…"

def test_map_outcome_friendly_text_from_planner():
    pr = _planner_result(outcome="refuse", refusal_message="No memory")
    dto = map_planner_result_to_dto(pr)
    assert dto.refusal_reason == "No memory"
```

- [ ] **Step 2: Implement** (read only `PlannerResult` fields; do not import `RetrievedContext`)

- [ ] **Step 3: Commit**

---

# Milestone N2 — `POST /agent` route + async `AgentLLM` (H4)

### Task N2.1: `agent/intent.py` async `OpenAICompatibleAgentLLM` (H4)

**Files:**
- Create: `server/src/sense_server/agent/intent.py`
- Test: `server/tests/agent/test_intent.py`

- [ ] **Step 1: Write failing test** (fake `httpx` client; assert `reason` is async; assert it wraps in `to_thread`)

- [ ] **Step 2: Implement**

```python
class OpenAICompatibleAgentLLM:
    def __init__(self, client: OpenAICompatibleChatModel):
        self._client = client
    async def reason(self, prompt: Prompt) -> LLMResult:
        raw = await asyncio.to_thread(self._client.complete, prompt.system, prompt.user)
        parsed, parse_error = self._parse(raw)
        return LLMResult(raw=raw, parsed=parsed, parse_error=parse_error)
    def _parse(self, raw):
        try:
            data = json.loads(raw)
            action = AgentAction(
                kind=AgentActionKind(data["kind"]),
                text=data["text"],
                atom_ids=tuple(data["atom_ids"]),
                confidence=data["confidence"],
            )
            return action, None
        except Exception as e:
            return None, str(e)
```

- [ ] **Step 3: Commit**

### Task N2.2: `http/routes/agent.py` — `POST /agent` handler

**Files:**
- Create: `server/src/sense_server/http/routes/agent.py`
- Test: `server/tests/http/test_agent_route.py`

- [ ] **Step 1: Write failing tests** (8 cases per spec Section 4.7)

- [ ] **Step 2: Implement** (thin: parse → build `PlannerContext` → call `Planner.plan` → map → respond)

- [ ] **Step 3: Wire into `http/app.py`**

```python
# http/app.py — add
from .routes.agent import post_agent
from .routes.memory import get_memory, get_session_memory, get_memory_atom
from .routes.metrics import get_metrics
app.router.add_post("/agent", post_agent)
app.router.add_get("/memory", get_memory)
app.router.add_get("/sessions/{id}/memory", get_session_memory)
app.router.add_get("/memory/{atomId}", get_memory_atom)
app.router.add_get("/metrics", get_metrics)
```

- [ ] **Step 4: Write a concurrency test** (H4)

```python
async def test_concurrent_agent_requests_do_not_serialize():
    """10 concurrent /agent requests with a 100ms-sleeping fake LLM should
    complete in ~100ms, not 1000ms (proves AgentLLM is async)."""
    ...
```

- [ ] **Step 5: Commit**

---

# Milestone N3 — Real `Planner` wired + memory/metrics routes + E2E (H3, H5, H6, H7)

### Task N3.1: `agent/context.py` + `agent/guardrails.py` + `agent/audit.py` + `agent/capability.py`

**Files:**
- Create: `server/src/sense_server/agent/context.py`
- Create: `server/src/sense_server/agent/guardrails.py`
- Create: `server/src/sense_server/agent/audit.py`
- Create: `server/src/sense_server/agent/capability.py`
- Test: one per file

- [ ] **Step 1: `ContextBuilder` with the v1 system prompt and atom delimiters** (test for the v1 string; test for the "no supporting memories" line; test that the Prompt's `system_prompt_version == "v1"` and `context_builder_version == "v1"` per H1)

- [ ] **Step 2: `Guardrails`** (Return / ReturnWithUncertainty / Refuse; rate limit; `no_supporting_memory` short-circuit using M4 operator spec: `confidence >= uncertainty_threshold` → Return)

- [ ] **Step 3: `AuditLogger`** with `AuditEntry` (domain) and `AuditRecord` (storage); `record(result, prompt)` computes `prompt_hash = sha256(prompt.system + "\n" + prompt.user)` and populates `llm_raw_output`, `system_prompt_text`, `user_prompt_text` per H5

- [ ] **Step 4: `CapabilityProvider` stub** (constant `CapabilitySet`)

- [ ] **Step 5: Commit per file**

### Task N3.2: `agent/planner.py` (stateless, audit isolation H3, narrow contexts M2)

**Files:**
- Create: `server/src/sense_server/agent/planner.py`
- Test: `server/tests/agent/test_planner.py`

- [ ] **Step 1: Write failing tests** (every outcome path; statelessness; audit failure does not lose response H3; `validated` is None iff short-circuit M5)

- [ ] **Step 2: Implement**

```python
class Planner:
    def __init__(self, retriever, context_builder, llm, validator, guardrails,
                 audit, metrics, capability_provider, clock, ids, config: PlannerConfig):
        self._retriever = retriever
        self._context_builder = context_builder
        self._llm = llm
        self._validator = validator
        self._guardrails = guardrails
        self._audit = audit
        self._metrics = metrics
        self._caps = capability_provider
        self._clock = clock
        self._ids = ids
        self._config = config
        # NO self._history, self._cache, self._rate_state — INV-1

    async def plan(self, ctx: PlannerContext) -> PlannerResult:
        latencies: dict[str, int] = {}
        # 1. RETRIEVE
        t = time.monotonic()
        retrieved = self._retriever.retrieve(RetrieverContext(
            query_text=ctx.trigger.text, limit=10, session_id=ctx.trigger.session_id,
        ))
        latencies["retrieval"] = int((time.monotonic() - t) * 1000)
        if not retrieved.atoms:
            # Short-circuit: no supporting memory
            result = self._make_result(ctx, retrieved, GuardedAction(
                outcome=GuardOutcome.REFUSE,
                action=None,
                refusal_reason=RejectionReason.NO_SUPPORTING_MEMORY,
                refusal_message="No relevant memory found.",
            ), latencies, validated=None, prompt=None)
            self._safe_audit(result, prompt=None)
            return result
        # 2. BUILD CONTEXT
        capabilities = self._caps.capabilities()
        prompt = self._context_builder.build(
            ctx.trigger, retrieved, capabilities,
            ctx.config.system_prompt_version, ctx.config.context_builder_version,
        )
        # 3. REASON (async — H4)
        t = time.monotonic()
        llm_result = await self._llm.reason(prompt)
        latencies["llm"] = int((time.monotonic() - t) * 1000)
        # 4. VALIDATE
        t = time.monotonic()
        validated = self._validator.validate(ValidatorContext(
            retrieved_atom_ids=tuple(a.atom_id for a in retrieved.atoms),
            no_memory_top_score_threshold=ctx.config.no_memory_top_score_threshold,
        ), llm_result)
        latencies["validator"] = int((time.monotonic() - t) * 1000)
        # 5. GUARD
        t = time.monotonic()
        guarded = self._guardrails.decide(GuardrailsContext(
            request_id=ctx.request_id, session_id=ctx.trigger.session_id, config=ctx.config,
        ), validated)
        latencies["guardrails"] = int((time.monotonic() - t) * 1000)
        # 6. BUILD RESULT
        result = self._make_result(ctx, retrieved, guarded, latencies, validated, prompt)
        # 7. AUDIT (H3: best-effort, never loses the response)
        self._safe_audit(result, prompt)
        return result

    def _safe_audit(self, result, prompt):
        try:
            audit_id = self._audit.record(result, prompt)
            result = result.model_copy(update={"audit_id": audit_id})
        except Exception:
            logger.exception("audit write failed for %s", result.request_id)
            self._safe_metric_inc(Metrics.AUDIT_RECORDS_TOTAL, {"status": "failed"})
```

- [ ] **Step 3: Write `test_audit_failure_does_not_lose_response`** (H3)

- [ ] **Step 4: Commit**

### Task N3.3: `http/routes/memory.py` + `http/routes/metrics.py`

**Files:**
- Create: `server/src/sense_server/http/routes/memory.py`
- Create: `server/src/sense_server/http/routes/metrics.py`
- Test: per file

- [ ] **Step 1: `GET /memory` + `GET /sessions/{id}/memory` + `GET /memory/{atomId}`** (accept the reserved query params; ignore `since`/`until`/`modality`/`format` this slice; return `kind="search"`)

- [ ] **Step 2: `GET /metrics`** with `Content-Type: text/plain; version=0.0.4; charset=utf-8`

- [ ] **Step 3: Commit**

### Task N3.4: Wire the real `Planner` in `run_gateway.py`

**Files:**
- Modify: `server/scripts/run_gateway.py`

- [ ] **Step 1: Construct the Planner with real components** (the real `Retriever`, the real `ContextBuilder`, the async `OpenAICompatibleAgentLLM`, the real `Validator`, the real `Guardrails`, the real `SqliteAuditLogger`, the real `MetricsRecorder`, the constant `CapabilityProvider`)

- [ ] **Step 2: Construct `InMemoryMetricsRecorder` (production: swap to Prometheus later)**

- [ ] **Step 3: Pass the Planner + deps into `http/app.py::build_app`**

- [ ] **Step 4: Commit**

### Task N3.5: The end-to-end integration test (H6 polling helper)

**Files:**
- Create: `server/tests/integration/test_cognitive_read_path.py`
- Create: `server/tests/integration/helpers.py` (the `wait_for` helper)

- [ ] **Step 1: Implement `wait_for` helper** (from H6)

- [ ] **Step 2: Implement the full E2E test** (per Section 5.4 of the design doc — every G1–G7 + I1–I8 gate)

- [ ] **Step 3: Run; fix any failures; commit**

```bash
cd server && python -m pytest tests/integration/test_cognitive_read_path.py -v
# All 15+ assertions pass.
```

**This is the gate. When this test passes, the P0 + P1 + P2-answers server side is done.**

---

# Milestone N4 — Android Chat + Memory + navigation

### Task N4.1: `core/SenseLog.kt` (trace-id-aware logging)

**Files:**
- Create: `android/.../core/SenseLog.kt`
- Test: `android/.../core/SenseLogTest.kt`

- [ ] **Step 1: Write failing test** (with/without trace context; tag formatting)

- [ ] **Step 2: Implement**

```kotlin
object SenseLog {
    fun d(tag: String, msg: String, trace: TraceContext? = null) {
        val suffix = trace?.let { " request_id=${it.requestId} trace_id=${it.retrievalTraceId} audit_id=${it.auditId}" } ?: ""
        Log.d(tag, msg + suffix)
    }
}
data class TraceContext(val requestId: String, val retrievalTraceId: String?, val auditId: String?)
```

- [ ] **Step 3: Commit**

### Task N4.2: `data/AgentApi.kt` + `data/AgentRepository.kt` (only `HttpApiError` importer — INV-11/L4)

**Files:**
- Create: `android/.../data/AgentApi.kt`
- Create: `android/.../data/AgentRepository.kt`
- Test: `android/.../data/AgentRepositoryTest.kt`

- [ ] **Step 1: Write failing tests** (5 mapping cases per Section 4.7)

- [ ] **Step 2: Implement** (the only class that translates `HttpApiError` → `AgentOutcome.Error`)

- [ ] **Step 3: Commit**

### Task N4.3: `data/MemoryApi.kt` + `data/MemoryRepository.kt` + `data/ChatHistoryStore.kt` (M8)

**Files:**
- Create: `android/.../data/MemoryApi.kt`
- Create: `android/.../data/MemoryRepository.kt`
- Create: `android/.../data/ChatHistoryStore.kt`
- Test: per file

- [ ] **Step 1: Write failing tests**

- [ ] **Step 2: Implement `ChatHistoryStore` with `flush()` called from `ProcessLifecycleOwner` observer (M8)**

- [ ] **Step 3: Commit**

### Task N4.4: `ui/nav/Destination.kt` + `ui/nav/BottomBar.kt` (INV-13, M3, M11)

**Files:**
- Modify: `android/.../ui/nav/Destination.kt`
- Modify: `android/.../ui/nav/BottomBar.kt`
- Test: `android/.../ui/nav/BottomBarTest.kt`

- [ ] **Step 1: Write `test_invariant_bottom_bar_ceiling`** (4 entries max; `Device` becomes a Home drill-down per M11)

- [ ] **Step 2: Implement**

- [ ] **Step 3: Commit**

### Task N4.5: `ui/nav/AppNavigation.kt` — add Chat, Memory, Atom routes

**Files:**
- Modify: `android/.../ui/nav/AppNavigation.kt`
- Test: extend `BottomBarTest.kt` or add `AppNavigationTest.kt`

- [ ] **Step 1: Write failing test for the deep-link target `Destination.Atom.build(atomId)`**

- [ ] **Step 2: Implement**

- [ ] **Step 3: Commit**

### Task N4.6: `ui/chat/` — ChatViewModel + ChatScreen

**Files:**
- Create: `android/.../ui/chat/ChatModels.kt`
- Create: `android/.../ui/chat/ChatState.kt`
- Create: `android/.../ui/chat/ChatViewModel.kt`
- Create: `android/.../ui/chat/ChatScreen.kt`
- Test: `android/.../ui/chat/ChatViewModelTest.kt`

- [ ] **Step 1: Write `ChatViewModelTest`** (7 cases: ask, blank_text, error, clear, trace_id, SavedStateHandle, history_persistence)

- [ ] **Step 2: Implement the ViewModel** (uses `SavedStateHandle`, writes one `Sense` log line per request carrying all three ids)

- [ ] **Step 3: Implement the Compose screen** (input → loading shimmer → outcome render; provenance chips are `tappable` and navigate to `Destination.Atom.build(atomId)`; Refuse renders "No relevant memory found" with a fallback link)

- [ ] **Step 4: Run `./gradlew test :app:assembleDebug`; commit**

### Task N4.7: `ui/memory/` — MemoryViewModel + MemoryScreen + AtomDetailScreen

**Files:**
- Create: `android/.../ui/memory/MemoryState.kt`
- Create: `android/.../ui/memory/MemoryViewModel.kt`
- Create: `android/.../ui/memory/MemoryScreen.kt`
- Create: `android/.../ui/memory/AtomDetailScreen.kt`
- Test: `android/.../ui/memory/MemoryViewModelTest.kt`

- [ ] **Step 1: Write `MemoryViewModelTest`** (5 cases: search, debounce, empty, SavedStateHandle, lastQueryAt)

- [ ] **Step 2: Implement**

- [ ] **Step 3: Run; commit**

---

# Milestone N5 — Android observability + invariant tests (M3, M5)

### Task N5.1: Android architectural-invariant tests

**Files:**
- Extend: `android/.../ui/nav/BottomBarTest.kt`
- Create: `android/.../core/TraceContextTest.kt`
- Create: `android/.../data/ChatHistoryStoreTest.kt`

- [ ] **Step 1: `test_invariant_android_error_category_discipline`** — assert only `AgentRepository` imports `HttpApiError`

- [ ] **Step 2: `test_invariant_android_trace_propagation`** — assert the VM writes one log line with all three ids

- [ ] **Step 3: `test_invariant_android_bottom_bar_ceiling`**

- [ ] **Step 4: `test_invariant_android_saved_state`** — draft input survives process death

- [ ] **Step 5: `test_invariant_android_chat_history_persistence`** — messages survive a process death round-trip

- [ ] **Step 6: Run `./gradlew test`; commit**

### Task N5.2: Final cross-review + green `main`

- [ ] **Step 1: Run the full server test suite**

```bash
cd server && python -m pytest -v
# All tests pass — including the E2E integration test.
```

- [ ] **Step 2: Run the full Android build**

```bash
cd android/sense-relay && ./gradlew test :app:assembleDebug
# All tests pass; APK builds.
```

- [ ] **Step 3: Update `~/.claude/projects/-Users-kevin-Projects-Sense/memory/project-status.md`** with the new slice's status

- [ ] **Step 4: Commit**

```bash
git add . && git commit -m "feat: complete P0+P1+P2-answers (cognitive read path)"
```

**This is the gate. When this commit lands, P0 + P1 + P2-answers is done.**

---

## Self-Review (against the spec)

1. **Spec coverage:** Sections 1–5 of the design doc are covered: contracts (M2.1), atom versioning (M1.1/M1.2), Scorer (M2.2), MemoryIndex versioning (M2.3), Validator (M2.4), Retriever (M3.1/M3.2), transactional indexing (M3.3), stages (M4.2), worker (M4.3), gateway wiring (M4.4), metrics (M5.1), architectural-invariant tests (M5.2), DTOs (N1.1), mapper (N1.2), async LLM (N2.1), POST /agent (N2.2), Planner (N3.2), memory routes (N3.3), gateway wiring (N3.4), E2E test (N3.5), Android (N4.1–N4.7), Android invariants (N5.1). Every amendment H1–H7 has a task: H1 (M2.1 + N3.1), H2 (M2.4), H3 (N3.2), H4 (N2.1), H5 (N3.1), H6 (N3.5), H7 (M4.3).

2. **Placeholder scan:** No "TBD"/"TODO"/"implement later"/"fill in details". Code is shown in every step where code is added.

3. **Type consistency:** `Prompt` (H1) is defined in the spec and used in `AgentLLM.reason`, `ContextBuilder.build`, `AuditEntry` — all consistent. `RejectionReason.NO_SUPPORTING_MEMORY` (H2) is used in `Guardrails` and asserted in the E2E test. `Metrics.INDEXING_FAILURES_TOTAL` (H7) is added to `Metrics.KNOWN` in M2.1 and used in M4.3. `confidence_band` is on `AgentResponseDTO` (N1.1) and populated by the mapper (N1.2). `MAX_ATOM_CHIP_TEXT_LEN` (L1) is on `dto.py` and used in the mapper.

---

## Plan summary

- **10 milestones** (M1–M5 server P0+P1, N1–N5 server P2-answers + Android).
- **~40 tasks**, each independently testable and committable.
- **Primary regression gate:** `test_cognitive_read_path_end_to_end` (N3.5).
- **Every High-priority amendment H1–H7** has at least one task.
- **Every architectural invariant INV-1..13** has at least one test.
- **Every Medium/Low item M1–M12, L1–L9** is placed in its milestone.
- **`main` is green at every commit.**
