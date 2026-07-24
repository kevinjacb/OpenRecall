# Sense — Agent + Memory Design (the intelligence layer)

> **Architectural principle (binding):** *The server owns intelligence. The wearable owns sensing and execution.* All reasoning, planning, memory extraction, retrieval, command generation, policy enforcement, and auditing occur on the server. The firmware remains **deterministic and stateless with respect to AI behavior** — it never makes autonomous decisions based on captured audio or transcription. Its responsibilities are limited to: **capture, buffer, execute validated commands, report status, acknowledge execution.** This principle guides every decision below.

**Goal:** Turn the existing capture→transcript→event loop into a capable, autonomous, debuggable AI-memory + control system — ambient memory the user can query, and an agent that can command the device (camera/audio/buffer) — with a single unified, audited decision pipeline and a pluggable, contract-first architecture.

**Status going in (already on `main`, working):** firmware audio capture/VAD/Opus/BLE-drain (MTU-sized §C.6), phone relay (BLE↔WS), gateway (reassemble→decode→MLX-whisper→transcript), §F event store + SessionIndex, HTTP API (`/status`, `/sessions`, `/sessions/{id}`, `/sessions/{id}/events`, `/health`, `/provisioning/pubkey`, bearer auth), Android UI (setup, home/dashboard, recordings/session-detail, device, settings), signed-command transport (sign→deliver→verify→ack, idempotent), gateway observability.

**Status going in (built, not wired):** `ExtractionPipeline`, `Extractor`, `Embedder`, `AtomStore`, `MemoryIndex`, `Retriever`, `CommandDispatcher` (issue/ack/pending), `Command` model (8 types), firmware `commands.c:execute()` (stub), `vision/pipeline.py`.

**Tech stack:** Python 3.12 / aiohttp / pydantic 2 (server); Kotlin + Compose (Android); ESP-IDF 5.1.6 / NimBLE 1.6 (firmware); OpenAI-compatible LLM (local `mlx_lm` default, cloud via config); SQLite (events, atoms, audit).

---

## 1. Architecture — one unified decision pipeline

```
  TRIGGER (user POST /agent  |  proactive transcript event, later)
     │
     ▼
  Planner ──► Retriever ──► ContextBuilder ──► AgentLLM ──► Validator ──► Guardrails ──► Dispatcher ──► AuditLogger
                            (memory)           (reason)     (schema +      (policy)      (issue)        (every step,
                                                                               confidence +                                provenance)
                                                                               command)
     │
     ▼
  Command (lifecycle) ──§E pending──► device execute ──ack──► AuditLogger (close loop)
```

**Unification rule (binding):** there is exactly one decision pipeline. User-initiated and (future) proactive transcript-triggered actions differ **only in the trigger source**. Everything after the trigger is identical — same components, same guardrails, same audit, same retries. This guarantees consistent behavior, safety, and maintainability.

Every component depends only on **interface** Protocols (Phase 0, §3). Real implementations are injected; tests inject fakes. No component imports another's implementation — only its Protocol.

---

## 2. Phase 0 — Contracts first

Before any behavior, define stable interfaces + data types and ship stub implementations. All later work depends on interfaces only. This stabilizes the architecture and makes every component independently testable/swappable.

### Data types (`contracts/types.py`)

- `Trigger` — `UserRequest(session_id, text)` | `Proactive(session_id, event_id, transcript)` (only the source differs).
- `Confidence(value: float)` — LLM-reported intent confidence ∈ [0,1].
- `AgentAction` — `Answer(text: str, atom_ids: list[str], confidence)` | `IssueCommand(command: Command, confidence)` | `Refuse(reason: str, confidence)`.
- `Command` (extends existing) — adds `idempotency_key: str`, `status: CommandStatus`, `created_at`, `lifecycle history`. Existing fields (`command_id`, `session_id`, `type`, `params`, `issued_at`, `expires_at`) unchanged.
- `CommandStatus` (lifecycle, §8): `Pending → Validated → Issued → Delivered → Executing → Completed | Failed | Cancelled | TimedOut`.
- `MemoryAtom` (versioned, §7): `event_id, session_id, seq, text, provenance, extraction_version, embedding_model, embedding_version, extractor_prompt_version, created_at`.
- `CapabilitySet` — `{camera, microphone, retrospective_buffer, display, speaker}` (device-advertised booleans).
- `DeviceResourceStatus` — `{battery_pct, storage_free_bytes, camera_available, microphone_available, recording: bool, relay_connected}`.
- `Metric` — `{name, value, tags, ts}` (counters/histograms, §10).

### Interface Protocols (`contracts/interfaces.py`)

```python
class MemoryStore(Protocol):           # AtomStore + MemoryIndex behind one seam
    def record(self, atom: MemoryAtom) -> None: ...
    def get(self, atom_id: str) -> MemoryAtom | None: ...
    def search(self, query: AtomQuery, limit: int) -> list[ScoredAtom]: ...

class Scorer(Protocol):               # pluggable retrieval scoring, §9
    def score(self, query: AtomQuery, atom: MemoryAtom) -> float: ...

class Retriever(Protocol):            # uses MemoryStore + Scorer
    def retrieve(self, query: AtomQuery, limit: int) -> list[ScoredAtom]: ...

class ContextBuilder(Protocol):       # builds the LLM prompt from retrieved atoms + trigger
    def build(self, trigger: Trigger, atoms: list[ScoredAtom]) -> Prompt: ...

class AgentLLM(Protocol):             # reasoning; OpenAI-compatible
    def reason(self, prompt: Prompt) -> LLMResult: ...   # LLMResult = raw + parsed AgentAction + Confidence

class Validator(Protocol):            # schema + confidence + command structure
    def validate(self, result: LLMResult) -> ValidatedAction: ...

class CapabilityProvider(Protocol):   # device capabilities + resource status
    def capabilities(self, session_id: str) -> CapabilitySet: ...
    def resources(self, session_id: str) -> DeviceResourceStatus: ...

class Guardrails(Protocol):            # policy: rate-limit, allowlist, params, capability, resource, confidence, idempotency
    def decide(self, action: ValidatedAction, ctx: TriggerContext) -> GuardedAction: ...

class CommandDispatcher(Protocol):     # issue + lifecycle transitions + idempotency
    def issue(self, command: Command) -> Command: ...      # Pending→Issued
    def transition(self, command_id: str, status: CommandStatus, detail: dict = {}) -> Command: ...
    def pending(self) -> list[Command]: ...

class AuditLogger(Protocol):           # every decision, provenance, lifecycle transitions
    def record(self, entry: AuditEntry) -> None: ...

class MetricsRecorder(Protocol):       # structured metrics, §10
    def count(self, name: str, tags: dict = {}) -> None: ...
    def observe(self, name: str, value: float, tags: dict = {}) -> None: ...

class Planner(Protocol):               # orchestrates the pipeline
    def plan(self, trigger: Trigger) -> PlanResult: ...   # PlanResult = Answer | CommandIssued | Refused (+ audit ids)
```

Each ships a **stub** first (in-memory / no-op / fixed-answer), behind which the real implementations land phase by phase. Tests depend on Protocols; production wires reals.

---

## 3. Components (single-responsibility, independently testable)

| Component | Responsibility | Real impl (phase) |
|---|---|---|
| **Planner** | Orchestrates the pipeline: trigger → retrieve → context → reason → validate → guardrails → dispatch/answer → audit. Holds no logic of its own. | `agent/planner.py` (P2) |
| **Retriever** | Finds relevant atoms via `MemoryStore`+`Scorer`. | `memory/retrieval.py` (P1, exists) |
| **ContextBuilder** | Builds the LLM prompt (system role, retrieved atoms with provenance, trigger text, available capabilities + resources, current time). | `agent/context.py` (P2) |
| **AgentLLM** | One LLM call; returns raw text + parsed `AgentAction` + `Confidence`. OpenAI-compatible. | `agent/intent.py` (P2) |
| **Validator** | Strict JSON-schema validation of the LLM output; confidence parse; command structure + param bounds. Invalid → `Refuse`. | `agent/validator.py` (P2) |
| **Guardrails** | Policy gate (§6). Decides autonomous / confirm / refuse; capability + resource checks; idempotency. | `agent/guardrails.py` (P2/P3) |
| **CommandDispatcher** | Issues commands, drives the lifecycle, dedups by idempotency key, exposes pending for the §E delivery path. | `commands/dispatcher.py` (exists, extend P3) |
| **AuditLogger** | Append-only audit of every pipeline step + lifecycle transition. | `agent/audit.py` (P2) |
| **CapabilityProvider** | Reads device-advertised capabilities + resource status (from a device status char / last-known cache). | `commands/capability.py` (P3) |
| **MetricsRecorder** | Structured metrics. | `agent/metrics.py` (P1, lightweight) |
| **Scorer** | Pluggable retrieval scoring. | `memory/scoring.py` (P1) |
| **MemoryStore** | Atom persistence + vector index. | `memory/store.py` (exists, extend P1) |
| **ExtractionWorker** | Out-of-band §F→§G sweep, resumable cursor, exactly-once. | `memory/worker.py` (P1) |

---

## 4. Memory — versioned atoms + out-of-band extraction

- **Extraction** runs out-of-band (LLM is slow; never on the audio hot path — the existing `ExtractionPipeline` docstring already mandates this). A periodic asyncio task in `run_gateway.py` calls `ExtractionPipeline.extract_new(session_id)`: reads §F events past the per-session cursor → `Extractor` (LLM) → `Embedder` → `MemoryStore.record(atom)` → advance cursor. Per-event try/catch (rethrow `CancellationException`); cursor advances even on failure → exactly-once, resumable.
- **Versioned atoms** carry `extraction_version`, `embedding_model`, `embedding_version`, `extractor_prompt_version`, `created_at`. When the pipeline improves (new extractor prompt / embedding model), selective re-extraction is driven by filtering on these versions — re-embed without re-running the extractor when only the model changed, etc.
- **Memory surface**: `GET /memory?q=&session=&limit=` (retrieve) and `GET /sessions/{id}/memory` (atoms for a session), bearer-auth, reusing `Retriever`/`MemoryStore`.

---

## 5. Retrieval — pluggable scoring

`Scorer` is a Protocol (`contracts/interfaces.py`). The `Retriever` asks the `MemoryStore` for candidates then ranks via the injected `Scorer`. **Initial impl: `SimRecencyScorer`** (semantic similarity + recency decay). The interface leaves room for future signals — importance, frequency, session affinity, user preference, confidence — without touching the `Retriever` or `Planner`. No retrieval logic is hardcoded outside a `Scorer`.

---

## 6. Guardrails (full autonomy, but safe + debuggable)

Autonomy is **confidence-gated**, not blanket. Policy:

| Confidence | Action |
|---|---|
| `>= 0.85` | autonomous execution |
| `0.60 – 0.85` | request confirmation (the single non-autonomous path; surfaced in UI/audit) |
| `< 0.60` | refuse |

Thresholds are **configurable** (`SENSE_CONFIDENCE_AUTONOMOUS`, `SENSE_CONFIDENCE_CONFIRM`, default 0.85/0.60).

Guardrail set (each a pure, unit-testable check in `Guardrails.decide`):
1. **Rate limit** — N actions/min/session.
2. **Command allowlist** — only `capture_photo`, `record_video`, `request_buffer`, `start_audio`, `stop_audio` autonomously executable (no `display_text`/`play_audio` — no hardware).
3. **Param bounds** — e.g. `record_video.duration_s ∈ [1,30]`; reject out-of-range.
4. **Confidence gate** — autonomous/confirm/refuse per above.
5. **Idempotency** — every command carries an `idempotency_key`; repeated requests with the same key return the existing command (never duplicate-execute).
6. **Capability negotiation** — the device advertises its `CapabilitySet`; the planner refuses a command the device doesn't support **before** dispatch.
7. **Resource validation** — check `DeviceResourceStatus` (battery, storage, camera/mic availability, already-recording, relay connectivity); refuse commands guaranteed to fail.

A guardrail refusal is an `AuditEntry` + a `Refuse` result — never a silent drop.

---

## 7. Commands as lifecycle objects

`Command` is a first-class domain entity with an explicit state machine:

```
Pending ──validate──► Validated ──issue──► Issued ──deliver(§E)──► Delivered
  ──execute──► Executing ──ack──► Completed
                    └─(err)──► Failed
          (abort)──► Cancelled     (timeout)──► TimedOut
```

- `CommandDispatcher.transition(command_id, status, detail)` is the only mutation path; every transition writes an `AuditEntry` and a `MetricsRecorder` observation.
- The lifecycle is reflected consistently in: dispatcher, audit, Android UI, and (future) retry — a `Failed`/`TimedOut` command carries enough detail to retry with a fresh `command_id` (same `idempotency_key` dedups).
- **Undo/abort**: `Cancelled` is reachable from `Pending`/`Validated`/`Issued`/`Delivered`/`Executing` (best-effort; the device honors an abort if it hasn't completed).

---

## 8. Observability — first-class

Structured **metrics** (`MetricsRecorder`), not just logs:

- Latencies: extraction, embedding, retrieval, LLM, command-issue, command-execution.
- Rates: retrieval hit-rate, refusal-rate (by guardrail), command success-rate, confidence distribution (histogram).
- Plus the existing `logging` (INFO flow / DEBUG detail) and the **audit log** (every decision with full provenance: trigger → retrieved atom ids → raw LLM output → validated action → guardrail decision → command_id + lifecycle).

Metrics are in-process (a simple counters/histograms recorder), surfaced via `GET /metrics` (Prometheus text format) for future scraping. Lightweight — no external metrics dependency.

---

## 9. Config (provider-agnostic)

```env
SENSE_LLM_PROVIDER=mlx|openai|ollama|...     # default: mlx (local)
SENSE_LLM_MODEL=...                           # default: a local mlx_lm model
SENSE_LLM_BASE_URL=...                        # OpenAI-compatible endpoint
SENSE_LLM_API_KEY=...                         # optional
SENSE_CONFIDENCE_AUTONOMOUS=0.85
SENSE_CONFIDENCE_CONFIRM=0.60
SENSE_RATE_LIMIT_PER_MIN=20
SENSE_EXTRACTION_INTERVAL_S=30
SENSE_WINDOW_MS=5000
```

`AgentLLM` + `Extractor` + `Embedder` share one OpenAI-compatible client built from these.

---

## 10. Firmware — deterministic sensor/actuator (no AI)

The firmware **never** reasons about audio/transcription. It:

- **Captures + buffers** (PDM→VAD→Opus→ring→§C.6→BLE) — done.
- **Executes validated commands** — `commands.c:execute()` implemented per type, deterministic:
  - `request_buffer` — mark the retrospective ring for backfill (no new hardware; easiest).
  - `start_audio`/`stop_audio` — gate the always-on VAD capture (force-record / pause).
  - `capture_photo`/`record_video` — OV5640 → JPEG/MP4 → SD over SPI; server retrieves via WiFi later (the existing "EOD video retrieval" path). Deferred to a later phase.
  - `display_text`/`play_audio` — **not implemented** (no display/speaker on XIAO Sense); the planner's capability check refuses them.
- **Reports status** — a status characteristic carries `CapabilitySet` + `DeviceResourceStatus` (battery, storage free, camera/mic availability, recording state). Read + notify.
- **Acks execution** — existing `command_ack` path, extended to include the lifecycle outcome (`Completed`/`Failed` + reason).

No LLM, no transcription, no decision logic on the device. Commands are verified (Ed25519), deduped by `command_id`, and executed exactly as specified.

---

## 11. Android — additive screens, reuse the design system

- **Chat screen** — NL input → `POST /agent` → renders `Answer` (with provenance chips) or `CommandIssued` (with live lifecycle status) or `Refuse` (with reason). Confidence-confirm path renders an accept/decline.
- **Memory screen** — browse atoms (`GET /memory`, `GET /sessions/{id}/memory`), search.
- **Command lifecycle** — a commands list (pending/executing/completed) pulling from audit/lifecycle.
- Reuses the existing design system, error/loading/empty patterns, `ConfigurationRepository`, `RelayController`. No rewrites of existing screens — additive only.

---

## 12. Testing (TDD, matching existing patterns)

- **Contracts** — Protocol conformance checks (every real impl satisfies its Protocol; a `isinstance` runtime_checkable guard in tests).
- **Pure cores** (Planner, Validator, Guardrails, ContextBuilder, Scorer, lifecycle state machine) — unit tests with fakes for `Retriever`/`AgentLLM`/`MemoryStore`/`CapabilityProvider`/`AuditLogger`/`MetricsRecorder`. Intent classification, command synthesis, every guardrail, every lifecycle transition, idempotency, confidence gating.
- **ExtractionWorker** — fake `EventStore`/`Extractor`: cursor advance, exactly-once, error isolation.
- **HTTP routes** — aiohttp `TestClient` (existing pattern) for `/agent`, `/memory`, `/metrics`.
- **Firmware executors** — host contract test where portable; on-device smoke for camera/SD.
- Every phase leaves `main` green (build + full test suite) and is cross-reviewed (the binding working style).

---

## 13. Phasing (each phase: contracts first, then impl, green main, cross-reviewed, independently demoable)

- **P0 — Contracts.** `contracts/` Protocols + types + stubs. Compiles, stubs return fixed answers. (Foundation; no behavior yet.)
- **P1 — Memory brain.** ExtractionWorker + versioned MemoryAtom + MemoryStore wiring + `SimRecencyScorer` + `/memory` routes + Android Memory screen + metrics stubs. **Demo: transcripts → browseable atoms.**
- **P2 — Agent (answers).** Planner + Retriever + ContextBuilder + AgentLLM + Validator + Guardrails(confidence/rate) + AuditLogger + `POST /agent` (Answer path) + Android Chat (answer + confirm). **Demo: "what did I say about X" → answer with provenance.**
- **P3 — Agent (commands, autonomous).** `IssueCommand` path + Guardrails(capability/resource/idempotency/params/allowlist) + CommandDispatcher lifecycle + §E delivery + device status/capability chars + Android command lifecycle UI. **Demo: "record the next 10s" → command issued, delivered, acked, lifecycle visible (device logs execution until executors land in P4).**
- **P4 — Device executors.** `request_buffer` → `start_audio`/`stop_audio` → `capture_photo`/`record_video` (OV5640 + SD + WiFi retrieval). **Demo: "record 10s" → actual video retrievable.**
- **P5 — Proactive seam.** A transcript-event trigger feeds the same pipeline (trigger source is the only difference, per the unification rule). **Demo: agent acts on ambient context.**

---

## 14. Non-goals / deferred

- `display_text`/`play_audio` executors (no hardware; capability check refuses).
- Proactive triggers (P5; reactive first per decision).
- Multi-tenant/multi-device (single device + single user assumed).
- Cloud LLM as the default (pluggable; local default).
- A polished mobile design pass on Chat/Memory (functional first; the existing design system applies).

---

## 15. Design principles (binding for the plan)

- **Server is intelligence; wearable is deterministic sensor/actuator.** No AI on the device, ever.
- **One decision pipeline**; trigger source is the only variable.
- **Contracts first** — depend on Protocols, not implementations.
- **Single responsibility per component**; each independently testable.
- **Full autonomy is confidence-gated** with configurable thresholds; refusals and confirms are audited, never silent.
- **Commands are lifecycle objects**; every transition audited + metricized.
- **Memory is versioned** for selective re-extraction.
- **Retrieval is pluggable** (Scorer Protocol).
- **Observability is first-class** (metrics + audit + logs).
- **No rewrites of working code** — extend/append; the existing gateway/relay/UI/firmware-capture paths are untouched.
- **No new runtime deps** without justification; prefer stdlib + the existing stack (aiohttp, pydantic, SQLite, OpenAI-compatible HTTP).