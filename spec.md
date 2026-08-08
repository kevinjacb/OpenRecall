# Spec — server endpoints for the OpenSapien app design

**Date:** 2026-08-08
**Input:** `findings.md` (design vs. server gap analysis, 5 blocking + 7 significant gaps)
**Target:** `server/src/opensapien_server/` at `d9d39ab`

This spec turns the gap list into an implementation plan, grounded in the actual
server architecture. Work is ordered into four phases; each endpoint section
states the wire contract, storage changes, and touched files.

---

## Architecture ground rules (apply to every endpoint)

These are the existing conventions; new code follows them.

1. **Framework / DI.** aiohttp. Each route module exposes `add_routes(app)`,
   lazily imported and called in `build_app` (`http/app.py:76-93`). Dependencies
   are optional `build_app` kwargs stored as `app["sense_*"]` keys. Handlers must
   guard `None` deps (existing tests call `build_app` with subsets).
2. **Auth.** The global `bearer_auth_middleware` covers every new route
   automatically; each new route module still gets a `*_requires_token` test.
3. **Wire convention.** The codebase has two: camelCase hand-rolled dicts
   (`/sessions`, `/status`, `/speakers`) and snake_case pydantic DTOs
   (`/memory`, `/agent`, `/commands`). Rule for new work: **extend a surface in
   the convention it already uses.** New memory routes → snake_case DTOs in
   `dto.py`; new session routes/fields → camelCase; new greenfield surfaces
   (`/settings`, `/device/status`) → snake_case DTOs (the DTO path is the
   stated direction; `ErrorEnvelopeDTO` for errors).
4. **Durability.** `SessionIndex`/`SessionSummary` are in-memory and rebuilt
   from the event store on restart (`sessions/index.py:176`), so **nothing
   durable may live only there.** Each new durable concern gets its own SQLite
   file via the `args.db.replace("events.db", "<name>.db")` idiom in
   `scripts/run_gateway.py:95-136`, with a `Sqlite*Store` class following the
   `SqliteEventStore` template (shared conn, `check_same_thread=False`,
   `threading.Lock`, `CREATE TABLE IF NOT EXISTS`) plus an `InMemory*Store`
   twin for tests.
5. **LLM calls from handlers/workers.** Reuse the shared
   `OpenAICompatibleChatModel` (`memory/llm.py`) via `asyncio.to_thread`,
   never blocking the loop, all exceptions caught into a degraded result —
   the `OpenAICompatibleAgentLLM` shape (`agent/intent.py:44-66`).
6. **Tests.** `tests/http/test_<module>.py`, bare `async def` (asyncio_mode
   auto), module-level `_client(tmp_path)` helper building a real app with
   in-memory stores, whole-dict wire assertions. NB: existing whole-dict
   assertions mean **any field added to an existing wire shape breaks existing
   tests** — update them in the same commit.

---

## Phase 1 — Memory read surface (unblocks the Memories tab)

Cheapest blocking work; no new subsystems.

### 1.1 `GET /memory` without `q` (list mode) — blocking #1

Make `q` optional. Two modes on one path:

- `q` present → existing semantic search, unchanged.
- `q` absent → **list mode**: reverse-chronological atom listing with
  `?kind=`, `?limit=` (default 20, clamp 100), `?cursor=`, `?session_id=`.

**Storage:** new `AtomStore` methods (both impls):

```python
def list(self, *, kind: str | None, session_id: str | None,
         limit: int, before: tuple[datetime, str] | None) -> list[MemoryAtom]
```

SQLite: `SELECT … ORDER BY created_at DESC, atom_id DESC LIMIT ?` with a
`(created_at, atom_id)` keyset predicate; add index
`ix_atoms_created (created_at, atom_id)` via the existing migration hook
(`memory/store.py:184`). Cursor: opaque base64url JSON
`{"before": iso, "last_id": atom_id}` — same scheme as `SessionIndex`
(`sessions/index.py:102-122`); malformed → 400 `bad_request`.

**Kind filter.** `MemoryAtom.kind` is a free string; observed values today:
`fact`, `task`, `scene` (+ whatever the extractor prompt allows). The design's
chips (Tasks / People / Decisions / Places / Preferences) don't match. Spec
decision: the endpoint filters on **raw stored kind, exact match, no
validation**; `GET /memory/stats` (below) tells the client which kinds exist,
and the client maps chips → kinds. Aligning the extractor taxonomy with the
design chips is follow-up work outside this spec.

**Wire:** new `MemoryListResponseDTO` in `dto.py`:
`schema_version, atoms: list[MemoryAtomDTO], next_cursor: str | null`.
Search mode keeps returning `MemorySearchResponseDTO` unchanged.

Touched: `memory/store.py`, `http/routes/memory.py`, `http/routes/dto.py`,
`tests/http/test_routes.py` (or new `test_memory_list.py`),
`tests/memory/` store tests.

### 1.2 `GET /memory/stats` — blocking #2

Response:

```json
{ "schema_version": "v1", "total": 128, "added_24h": 6,
  "by_kind": {"task": 41, "fact": 80, "scene": 7} }
```

**Storage:** `AtomStore.stats(*, now) -> AtomStats` — one
`GROUP BY kind` plus a `WHERE created_at >= now-24h` count. No caching needed
at current scale (brute-force cosine search is already O(n)).

Touched: `memory/store.py`, `http/routes/memory.py`, `dto.py`, tests.

---

## Phase 2 — Session metadata (titles, search, memory counts, settings, command create)

Unblocks Home, Recordings, Settings; all additive on existing shapes.

### 2.1 Session meta store + `title` — significant #6

New durable concern: **`session_meta.db`**, table
`session_meta(session_id TEXT PK, title TEXT, title_source TEXT
CHECK(title_source IN ('llm','user')), updated_at TEXT)`. New
`SessionMetaStore` protocol (`get`, `set_title`, `all_titles`, `delete`) with
SQLite + in-memory impls in a new `sessions/meta.py`. Wired as
`build_app(session_meta_store=…)` → `app["sense_session_meta"]`.

**LLM titling.** Hook the existing extraction finalize path: when
`GatewayCore._on_bye` enqueues session finalization, the extraction worker
additionally (if no title yet) prompts the shared `llm_chat` with the first ~30
transcript lines → ≤6-word title → `set_title(sid, title, source="llm")`.
Failure is silent (title stays null; client falls back to `preview`). Never
overwrite a `user`-sourced title.

**Wire:** `_summary_to_wire` (`http/routes/sessions.py:73`) gains
`"title": str | null`, resolved at read time from the meta store (mirrors how
`speakerName` is resolved from the speaker registry — derived index stays
title-free, satisfying the rebuild constraint). Update whole-dict assertions in
`test_sessions.py`.

**`PATCH /sessions/{id}` — rename.** Body `{"title": "…"}` (1–120 chars,
stripped; else 400). 404 on unknown session. Writes `source="user"`. Returns
the updated summary wire shape.

Touched: new `sessions/meta.py`, `http/app.py`, `http/routes/sessions.py`,
`memory/extraction_worker.py` (titling hook), `scripts/run_gateway.py`, tests.

### 2.2 `GET /sessions?q=` — transcript search — significant #7

Extend the existing route: when `q` is present, return sessions whose
transcript text matches, same wire shape (`{"sessions": [...], "nextCursor": null}`),
each row plus `"matchSnippet": str`.

**Storage:** `EventStore.search(q, *, limit) -> list[tuple[session_id, snippet]]`.
SQLite impl: `LIKE '%q%'` on `capture_events.text` (case-insensitive via
`LOWER()`), grouped by session, most-recent-first, capped at `limit` sessions.
No FTS5 in v1 — the table is small and `LIKE` keeps the in-memory impl trivial;
note in code that FTS5 is the upgrade path. Search mode is unpaginated
(`nextCursor: null`, limit clamp 100).

Touched: `events/store.py`, `http/routes/sessions.py`, tests.

### 2.3 `memoryCount` on session rows — significant #8

`AtomStore.counts_by_session() -> dict[str, int]` (one `GROUP BY session_id`).
`GET /sessions` fetches it once per request and stamps
`"memoryCount": int` into each row (0 when atom store absent). Kills the N+1.

Touched: `memory/store.py`, `http/routes/sessions.py`, tests.

### 2.4 `POST /commands` — blocking #5

HTTP create route so Settings toggles can issue `start_audio` / `stop_audio`.

**Request** (snake_case, matching the existing commands DTO surface):

```json
{ "type": "start_audio", "session_id": "…", "params": {},
  "idempotency_key": "…" }
```

**Flow — reuse the agent's validation chain, not a parallel one:**

1. Validate `type` against the `ALLOWLIST` + `_TYPE_SCHEMAS` in
   `agent/validator_command.py` (start/stop_audio take no params).
2. Apply `StrictCommandGuardrails` with the capability provider snapshot
   (battery floor etc.), exactly as `Planner._dispatch_command` does
   (`agent/planner.py:335-368`).
3. Build `Command` via `app["sense_id_generator"]`, clock now, 5-min TTL,
   pass-through `idempotency_key`; `dispatcher.issue(...)` (signs Ed25519,
   dedups on idempotency key, persists to `SqliteCommandStore`).

**Responses:** 201 with the existing command wire shape + `status: "PENDING"`;
validation failure → 400 `bad_request` with the validator's reason; guardrail
refusal → 403; missing deps (`command_store`/`dispatcher`/`id_generator`) →
route not registered (existing conditional in `app.py:92`).
Idempotent replays return the original command with 200.

Extract the shared issue path into a small helper (e.g.
`commands/issue.py: validate_and_issue(...)`) so planner and HTTP route call
the same code.

Touched: `http/routes/commands.py`, new `commands/issue.py`,
`agent/planner.py` (refactor to helper), tests
(`test_command_routes.py` + integration).

### 2.5 `GET` / `PUT /settings` — significant #9

Greenfield KV persistence: **`settings.db`**, table
`settings(key TEXT PK, value TEXT /* JSON */, updated_at TEXT)`; new
`settings/store.py` with `SettingsStore` protocol (`get_all`, `put`),
SQLite + in-memory impls, `app["sense_settings_store"]`.

v1 schema is a fixed typed document, not open KV over the wire:

```json
{ "schema_version": "v1",
  "capture": { "audio_enabled": true, "vision_enabled": false,
               "retention_days": 30 } }
```

`PUT /settings` accepts a full or partial document (pydantic DTO,
`extra="forbid"` → unknown keys 400), merges, persists, returns the merged
result. **Server behavior must follow the stored values** — at minimum,
`capture.audio_enabled=false` gates ingest (checked in
`AudioIngestPipeline.ingest` or gateway accept path) so the toggle isn't
cosmetic; wire this in the same PR or the setting silently diverges, which is
exactly the failure findings.md warns about.

**Explicitly out of scope:** a "wake word" setting. Per findings.md the design's
wake-word toggle contradicts the architecture (no wake-word engine exists).
The settings document simply has no such field until the product decision in
findings.md ("Conflicts") is made.

Touched: new `settings/store.py`, new `http/routes/settings.py`, `http/app.py`,
`scripts/run_gateway.py`, tests.

---

## Phase 3 — Audio plane (the long pole)

Blocking #3/#4. Today no audio survives ingest: Opus frames are decoded, fed to
the transcriber, and the PCM hop is `del`'d (`ingest/pipeline.py:159-160`).
`BlobStore` exists (`media/blob.py`) but is content-addressed with no
append/list — wrong shape for a growing per-session stream, so audio gets its
own store rather than forcing BlobStore to fit.

### 3.1 Persistence: `AudioStore` (new `media/audio.py`)

Tap point: `AudioIngestPipeline.ingest()` at `pipeline.py:143`, immediately
after reassembly and **before** decode — persist the raw ordered Opus frames
(16 kHz mono, 20 ms/frame). Storing Opus keeps writes tiny (~2–4 KB/s vs
32 KB/s PCM) and lossless w.r.t. what the device sent.

- **Format on disk:** one **Ogg Opus** file per session,
  `<data>/audio/<session_id>.ogg`, appended incrementally (Ogg pages flushed
  every ~1 s). Ogg Opus is the natural container (Android ExoPlayer and
  browsers play it natively; seekable; append-friendly). Muxing: `ogg`
  page-writing is small enough to implement directly (RFC 7845 headers +
  page CRC) — no new native dependency; frames are fixed 20 ms so granule
  positions are trivial.
- **API:** `append(session_id, frames: list[bytes])`, `finalize(session_id)`,
  `stat(session_id) -> AudioStat(byte_count, duration_ms) | None`,
  `open(session_id) -> path`, `delete(session_id)`.
- A pipeline flag (`persist_audio: bool`, driven by `capture.audio_enabled`
  from §2.5) controls the tap.

### 3.2 `GET /sessions/{id}/audio` — blocking #3

Serve the file with **HTTP Range support** — aiohttp's `web.FileResponse`
handles `Range`/`206`/`ETag` natively, so the handler is: resolve session →
404 if no audio → `FileResponse(path, headers={"Content-Type": "audio/ogg"})`.
For a still-recording session, serve current bytes (growing file; client
re-requests for more).

Also stop lying on the event wire: `_event_to_wire` fills
`codec: "opus"`, `sampleRateHz: 16000`, and real `byteCount` when the audio
store has the session (replacing the hardcoded zeros at
`http/routes/sessions.py:104-109`).

### 3.3 `GET /sessions/{id}/waveform` — blocking #4

Precompute during ingest — the decoded PCM hop is already in hand at
`pipeline.py:159`; computing `max(abs(sample))` per 500 ms bucket there is
nearly free. Persist buckets in the audio store sidecar
(`<session_id>.peaks.json`, appended with the same cadence as Ogg flushes).

Wire:

```json
{ "schemaVersion": "v1", "bucketMs": 500, "peaks": [0.12, 0.55, …],
  "durationMs": 61234 }
```

Client resamples N buckets → its 34 bars. 404 when no audio.

Touched: new `media/audio.py`, `ingest/pipeline.py`, `http/routes/sessions.py`,
`http/app.py`, `scripts/run_gateway.py`, `sim/` (simulator should exercise the
tap), tests (`tests/media/`, `tests/http/`, integration with the sim device).

**Risks / notes:** disk growth (~10–15 MB/hour of speech) — retention uses
`capture.retention_days` from §2.5 (a periodic sweep deleting audio for
sessions older than N days; events/atoms retention is a separate product
decision). Ogg muxer needs a golden-file test (decode with `opuslib` /
`ffprobe` in CI if available, else byte-level header assertions).

---

## Phase 4 — Device status & deletion

### 4.1 `GET /device/status` — significant #10

Two-stage, honestly labeled:

**Stage A (this spec):** expose the `CapabilityProvider` over HTTP:

```json
{ "schema_version": "v1", "source": "static",
  "battery_pct": 1.0, "storage_free_bytes": 1073741824,
  "recording": false, "relay_connected": true,
  "microphone_available": true, "camera_available": false,
  "firmware_version": null }
```

`source: "static" | "device"` tells the client whether numbers are real; the
UI shows "—" for battery when `source != "device"` instead of a fake 100 %.
`relay_connected`/`recording` can be made real immediately from
`SessionLifecycle` + gateway connection state.

**Stage B (separate work, firmware-dependent):** add a `telemetry` inbound
frame to `protocol/messages.py` (`battery_pct`, `storage_free_bytes`,
`firmware_version`), a `ReportedCapabilityProvider` that caches the last frame
(falling back to constants), and firmware support. This spec defines the
message shape but Stage B ships only with the firmware change.

Touched: new `http/routes/device.py`, `agent/capability.py`
(provider gains an optional live source), `http/app.py`, tests; Stage B:
`protocol/messages.py`, `gateway/core.py`, firmware.

### 4.2 `DELETE /sessions/{id}` — significant #11

Cascading, idempotent delete: events (`EventStore.delete_session` — new),
atoms (`AtomStore.delete_session` — new), vectors
(`MemoryIndex.delete_session` — new), audio + peaks
(`AudioStore.delete`), session meta, and `SessionIndex` entry (plus
re-derived counters). 204 on success, 404 unknown id, 409 if the session is
currently active (`SessionLifecycle.is_active`). Order: SQLite rows first,
files last, index entry last (a crash mid-delete must not resurrect the
session on `rebuild_from_store` — deleting events first guarantees that).

### 4.3 `DELETE /devices/{id}` — significant #12 — **deferred**

findings.md flags the semantics as unresolved ("Forget this Sense": local
unpair vs. server purge). v1 ships **local unpair only (no endpoint)**; a
server-side "purge all data" is a distinct, dangerous operation that deserves
its own confirmation design. Revisit after the product call.

---

## Explicitly out of scope (product decisions from findings.md)

- **Wake-word toggle** — contradicts the no-wake-word architecture; cut or
  redesign before any server work.
- **Speaker naming & Commands screens** — server support already exists
  (`/speakers/*`, `/commands`); the gap is the design dropping them, not the
  server. No server work either way.
- **Extractor kind taxonomy** (chips vs. stored kinds) — see §1.1.
- **Chat history persistence** — findings.md notes the design doesn't
  require it.

## Open questions for the user

1. **§1.1 kind taxonomy:** OK to ship raw kinds + client-side chip mapping,
   or should the extractor prompt be retargeted to the design's five
   categories now?
2. **§3 audio retention default:** proposed 30 days — confirm.
3. **§4.1 Stage B:** should the firmware telemetry frame be scheduled with
   this work or parked?

## Suggested sequencing & test gates

| Phase | Contents | Unblocks |
|---|---|---|
| 1 | memory list + stats | Memories tab |
| 2 | titles, search, memoryCount, POST /commands, settings | Home, Recordings, Settings |
| 3 | audio store, /audio, /waveform | Recording Detail player |
| 4 | device status (stage A), session delete | Settings header, ⋮ menu |

Each phase lands with: unit tests for new store methods (SQLite + in-memory
parity), HTTP tests per new/changed route (incl. 401 + whole-dict wire
assertions), and an updated integration test where the phase touches the
gateway path (audio tap, command create). Phase 3 is independent of 1–2 and can
start in parallel if two workstreams exist; 4.2 depends on 3 (audio delete) and
2.1 (meta delete).
