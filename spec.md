# Spec — server endpoints for the OpenSapien app design

**Date:** 2026-08-08
**Input:** `findings.md` (design vs. server gap analysis, 5 blocking + 7 significant gaps)
**Target:** `server/src/opensapien_server/` at `d9d39ab`
**Status:** decisions below are settled; one open question remains (§Open questions).

---

## Decisions taken (and why they overrule findings.md)

`findings.md` assumed **session == recording**. That assumption is false, and it
is the reason this spec is structured differently from the gap list.

| # | Decision | Rationale |
|---|---|---|
| D1 | **Introduce a `Segment` entity**; the app's list and detail pages address segments, not sessions | A session is minted by the Android relay in `onStartCommand` (`RelayService.kt:139`) and reused across every BLE/WS reconnect. Nothing ends it — no timer, no idle rollover, no day boundary. A session is "however long the foreground service lived": hours to days. The design shows discrete recordings ("Studio standup · 01:04"). Session ≠ recording. |
| D2 | **`/settings` is durable desired state; the server reconciles the device on connect** and gates ingest as a backstop | Commands are only delivered to a live gateway session, so a command-only toggle is dead whenever the device is offline — which is most of the time. |
| D3 | **Commands may be issued unbound** (`session_id` empty = "the device, whenever connected"); the gateway stamps the live session id at send | Session ids are relay-minted UUIDs never surfaced over HTTP, so an HTTP caller has nothing correct to put in the field. Firmware ignores `session_id` entirely (`commands.c` never reads it). |
| D4 | **Audio persists as a raw frame log; the Ogg container is built on read** | The original "hand-roll an incremental Ogg muxer on the ingest path" plan put RFC 7845 headers, page CRC and granule bookkeeping where a bug destroys data at write time. A length-prefixed frame log is trivial and crash-safe. |
| D5 | **Session time is the master clock: frame index `i` covers `[i*20, (i+1)*20)` ms**, gaps included | `SessionReassembler` drops late/duplicate packets and firmware VAD suppresses silence, so bytes-written ≠ time-elapsed. Without this invariant the playhead drifts from transcript `start_ms` and the detail page's core interaction breaks. |
| D6 | **Memory ordering is by conversation time (`occurred_at`), not `created_at`** | Extraction runs in batch after the fact, so `created_at` is when the server learned something. Last night's session extracted this morning would report as "added today", and all its atoms would share one timestamp. |
| D7 | **Three foundation defects are fixed first, in Phase 0** | Phases 2 and 5 are unsound on the current foundation (details in Phase 0). |
| D8 | **Tiered retention: audio expires (default 30 days), derived text is kept** | Deliberate choice, documented as such: audio is the bulky and most sensitive artifact; the derived text is the product. |
| D9 | **Firmware telemetry is parked. Battery ships as a 45 % placeholder; connection freshness is the real signal on the Settings header** | Battery sensing does not exist at any layer — no ADC channel, no fuel-gauge driver, no `vbat` reference anywhere in `firmware/`; the only mention is a planned investigation at `firmware/README.md:90`. So this is hardware bring-up, possibly a board revision, not a protocol change. Meanwhile the server already knows something true and useful about device health (§5.1). |

---

## Architecture ground rules (apply to every endpoint)

1. **Framework / DI.** aiohttp. Each route module exposes `add_routes(app)`,
   lazily imported and called in `build_app` (`http/app.py:76-93`). Dependencies
   are optional `build_app` kwargs stored as `app["sense_*"]` keys. Handlers
   must guard `None` deps — existing tests call `build_app` with subsets.
2. **Auth.** The global `bearer_auth_middleware` covers every new route
   automatically; each new route module still gets a `*_requires_token` test.
3. **Wire convention.** Two exist: camelCase hand-rolled dicts (`/sessions`,
   `/status`, `/speakers`) and snake_case pydantic DTOs (`/memory`, `/agent`,
   `/commands`). Rule: **extend a surface in the convention it already uses.**
   `/segments` mirrors `/sessions` → camelCase. Memory routes → snake_case
   DTOs. New greenfield surfaces (`/settings`, `/device/*`) → snake_case DTOs
   with `ErrorEnvelopeDTO` for errors.
4. **Durability.** `SessionIndex`/`SessionSummary` are in-memory, rebuilt from
   the event store on restart (`sessions/index.py:176`), so **nothing durable
   may live only there.** The same applies to the new `SegmentIndex`. Each
   durable concern gets its own SQLite file via the
   `args.db.replace("events.db", "<name>.db")` idiom
   (`scripts/run_gateway.py:95-136`), a `Sqlite*Store` following the
   `SqliteEventStore` template (shared conn, `check_same_thread=False`,
   `threading.Lock`, `CREATE TABLE IF NOT EXISTS`), and an `InMemory*Store`
   twin for tests.
5. **Derived ids must be deterministic.** Anything durable keyed by a derived
   entity needs an id that survives a rebuild. Segment ids follow the existing
   `event_id = f"{session_id}:{seq}"` precedent (`gateway/core.py:353`).
6. **LLM calls** reuse the shared `OpenAICompatibleChatModel` (`memory/llm.py`)
   via `asyncio.to_thread`, never blocking the loop, all exceptions caught into
   a degraded result — the `OpenAICompatibleAgentLLM` shape
   (`agent/intent.py:44-66`).
7. **Tests.** `tests/http/test_<module>.py`, bare `async def` (asyncio_mode
   auto), module-level `_client(tmp_path)` helper building a real app with
   in-memory stores. Existing whole-dict wire assertions mean **any field added
   to an existing response breaks existing tests** — update them in the same
   commit.

---

## Phase 0 — Foundation fixes

Three defects that later phases depend on. Small, and worth isolating from
feature review.

**0.1 — Lifecycle leak.** `SessionLifecycle.deregister` is called only from
`GatewayCore._on_bye` (`gateway/core.py:301`). `gateway/adapter.py:343-350`'s
`finally` block calls only `finalize_pending_session()`. So an abrupt
disconnect leaks the session id into `_active` forever, inflating
`activeSessions` in `/status` — and §5.2's "409 if active" would make exactly
those sessions permanently undeletable. This also contradicts
`sessions/lifecycle.py:9-12`, which documents a safety net that does not exist.
Fix: deregister in the adapter's `finally`.

**0.2 — Sessions never end.** `SessionSummary.mark_closed` has no production
caller, so `ended_at` is always `null` and `duration_ms` is forever computed
against `now`. Fix: call `mark_closed` from `_on_bye` and from the adapter's
`finally`.

**0.3 — `bye` is unreliable.** On a WebSocket drop the relay sends `bye` into
an already-dead socket (`RelayService.kt` `softDrop`), so the server usually
never sees it. Conversely a BLE-only drop sends a `bye` for a session the relay
fully intends to keep using. **Consequence for this spec: nothing may be
triggered by `bye` alone.** Segment close (§2.1) is therefore driven by a
server-side idle timer, and titling hangs off segment close rather than session
end.

Touched: `gateway/adapter.py`, `gateway/core.py`, `sessions/lifecycle.py`
(docstring), tests in `tests/gateway/`.

---

## Phase 1 — Memory read surface

Unblocks the Memories tab. Independent of every other phase.

### 1.1 `occurred_at` on atoms (D6)

Add `occurred_at: datetime` to `MemoryAtom` (`memory/atom.py`), computed at
extraction as `session_started_at + timedelta(milliseconds=start_ms)`.
Denormalized column on `memory_atoms` with index
`ix_atoms_occurred (occurred_at, atom_id)`, added through the existing
migration hook (`memory/store.py:184-187`). Backfill existing rows by joining
`start_ms` against the session's first event timestamp; rows whose session no
longer exists fall back to `created_at`.

Both `occurred_at` and `created_at` go on the wire — the header count and the
list order use `occurred_at`; `created_at` stays available for debugging.

### 1.2 `GET /memory` without `q` — list mode (blocking #1)

Make `q` optional; the path serves two modes:

- `q` present → existing semantic search, response unchanged
  (`MemorySearchResponseDTO`).
- `q` absent → **list mode**: `?kind=`, `?session_id=`, `?limit=` (default 20,
  clamp 100), `?cursor=`, ordered `occurred_at DESC, atom_id DESC`.

New `AtomStore.list(*, kind, session_id, limit, before)` on both impls. Cursor
is opaque base64url JSON `{"before": iso, "last_id": atom_id}` — the same
scheme as `SessionIndex` (`sessions/index.py:102-122`); malformed → 400
`bad_request`.

**Kind filter.** `MemoryAtom.kind` is a free string (`memory/atom.py:6-7`);
values in the wild are `fact`, `task` (`memory/extract.py:47-49`) and `scene`
(`vision/pipeline.py:59`). The design's chips (Tasks / People / Decisions /
Places / Preferences) do not match. This endpoint filters on the **raw stored
kind, exact match, no validation**; `/memory/stats` tells the client which
kinds exist and the client maps chips onto them. Retargeting the extractor
taxonomy is follow-up work, deliberately not bundled here.

New `MemoryListResponseDTO`: `schema_version, atoms: [MemoryAtomDTO],
next_cursor: str | null`.

### 1.3 `GET /memory/stats` (blocking #2)

```json
{ "schema_version": "v1", "total": 128, "added_24h": 6,
  "by_kind": {"task": 41, "fact": 80, "scene": 7} }
```

`AtomStore.stats(*, now)` — one `GROUP BY kind` plus a count with
`occurred_at >= now-24h`. No caching; brute-force cosine search is already O(n)
at this scale.

Touched: `memory/atom.py`, `memory/store.py`, `memory/extraction_worker.py`,
`http/routes/memory.py`, `http/routes/dto.py`, `tests/memory/`, `tests/http/`.

---

## Phase 2 — Segments (D1)

The app's primary surface. Absorbs findings.md #6 (title), #7 (search) and
#8 (memory count), all of which were specified against sessions and belong on
segments.

### 2.1 The segment model

A **segment** is a contiguous run of transcript activity within a session.
Derived from the ordered event stream by two rules:

- **Idle close:** no transcript event for `SEGMENT_IDLE_MS` (default 300 000 =
  5 min) → the segment closes at the end of its last event.
- **Hard cap:** a segment spanning `SEGMENT_MAX_MS` (default 3 600 000 = 60 min)
  force-closes; the next event opens a new one.

`segment_id = f"{session_id}:{first_seq}"` — deterministic, so a rebuild
reproduces identical ids and durable metadata keyed by them stays attached
(ground rule 5).

`SegmentIndex` mirrors `SessionIndex`: fed from `GatewayCore._emit` alongside
`SessionIndex.record` (`gateway/core.py:368`), with `rebuild_from_store` on
startup and the same base64url keyset cursor. Fields: `id, session_id,
started_at, ended_at, first_seq, last_seq, transcript_count, preview, closed`.

**Idle close needs a clock, not an event.** A periodic sweeper (reuse the
extraction worker's existing tick) closes segments whose last event is older
than the idle threshold, and fires the titling job. This is what makes segment
close survive a lost `bye` (§0.3).

**Firmware VAD interacts favourably here:** the device already suppresses
silence and emits gap markers (`vad.h:8`, `opensapien_sensor.c:82`), so
"transcript silence" and "no audio transmitted" broadly coincide. The segmenter
still keys off transcript events, which are robust to VAD tuning.

### 2.2 Segment metadata store + titles (significant #6)

New `segment_meta.db`:

```sql
CREATE TABLE IF NOT EXISTS segment_meta (
  segment_id  TEXT PRIMARY KEY,
  title       TEXT,
  title_source TEXT CHECK(title_source IN ('llm','user')),
  updated_at  TEXT
);
```

New `sessions/segment_meta.py` with `SegmentMetaStore` (`get`, `set_title`,
`titles_for`, `delete`), SQLite + in-memory impls, `app["sense_segment_meta"]`.

**Titling.** On segment close the sweeper enqueues a job: if no title exists,
prompt the shared `llm_chat` with the segment's transcript (first ~30 lines) →
≤6-word title → `set_title(segment_id, title, source="llm")`. Failure is
silent; the client falls back to `preview`. A `user` title is never
overwritten.

### 2.3 Segment routes

All camelCase, mirroring `/sessions`.

| Method | Path | Notes |
|---|---|---|
| GET | `/segments` | `?limit` `?cursor` `?q` `?session_id` |
| GET | `/segments/{id}` | summary + events |
| GET | `/segments/{id}/events` | |
| GET | `/segments/{id}/memory` | atoms whose `start_ms` falls in range |
| PATCH | `/segments/{id}` | `{"title": "…"}`, 1–120 chars stripped, writes `source="user"` |
| GET | `/segments/{id}/audio` | Phase 3 |
| GET | `/segments/{id}/waveform` | Phase 3 |
| DELETE | `/segments/{id}` | Phase 5 |

Row shape:

```json
{ "id": "…:412", "sessionId": "…", "title": "Studio standup",
  "startedAt": "…", "endedAt": "…", "durationMs": 64000,
  "transcriptCount": 37, "memoryCount": 4, "preview": "…",
  "hasAudio": true }
```

`/sessions` stays as-is for debugging and plumbing; the app does not use it.

### 2.4 Transcript search — `GET /segments?q=` (significant #7)

`EventStore.search(q, *, limit) -> list[(session_id, seq, snippet)]`. SQLite:
`LOWER(text) LIKE ?` on `capture_events` with `%`/`_` escaped in user input,
most recent first. The index maps `(session_id, seq)` → segment; results are
deduped to one row per segment with a `matchSnippet` field. Search mode is
unpaginated (`nextCursor: null`, limit clamp 100). No FTS5 in v1 — the table is
small and `LIKE` keeps the in-memory impl trivial; note FTS5 as the upgrade
path in a code comment.

### 2.5 `memoryCount` (significant #8)

Atoms carry `session_id` + `start_ms`, so a segment's count is a range query.
For a page of segments, fetch `(session_id, start_ms)` for the distinct
sessions on that page in one query and bucket in Python. One query per page,
not per row — the N+1 in findings.md #8 is gone either way.

Touched: new `sessions/segments.py`, new `sessions/segment_meta.py`, new
`http/routes/segments.py`, `events/store.py`, `memory/store.py`,
`memory/extraction_worker.py` (sweeper hook), `gateway/core.py`,
`http/app.py`, `scripts/run_gateway.py`, tests.

---

## Phase 3 — Audio plane (D4, D5)

Blocking #3/#4. Today nothing survives ingest: frames are decoded, fed to the
transcriber, and the PCM hop is `del`'d (`ingest/pipeline.py:159-160`).
`BlobStore` (`media/blob.py`) is content-addressed with no append — wrong shape
for a growing stream, so audio gets its own store.

### 3.1 The frame log

Tap point: `AudioIngestPipeline.ingest()` at `pipeline.py:143`, after
reassembly and **before** decode. Persist raw Opus frames (16 kHz mono,
20 ms/frame) to `<data>/audio/<session_id>.opusraw`.

Record format, one per 20 ms of **session time**:

```
[u16 len][payload]        len = 1..65534  → an Opus frame
[u16 0xFFFF][u32 count]   → a run of `count` silent 20 ms slots
```

The gap record is what makes D5 cheap. Firmware VAD suppresses silence, so an
always-on wearable produces long gaps; run-length encoding them costs 6 bytes
per gap instead of ~5 bytes per 20 ms slot. Gaps are written for all three
causes: VAD suppression (`VadState.GAP_MARKER`), packets the reassembler
dropped (`missing_range()`), and a `stop_audio` pause — firmware already emits
gap markers while paused specifically to keep `chunk_seq`/`rel_ts` contiguous
(`opensapien_sensor.c:76-85`).

**Invariant (D5):** slot index `i` covers `[i*20, (i+1)*20)` ms of session time,
always. This is what makes the playhead line up with transcript `start_ms`, and
it is the thing to assert in tests — feed a lossy packet stream through the sim
device and check that slot count equals elapsed session ms / 20.

API: `append(session_id, slots)`, `mark_gap(session_id, count)`,
`stat(session_id) -> AudioStat(slot_count, byte_count)`,
`read_range(session_id, start_ms, end_ms) -> Iterator[bytes]`,
`erase_range(session_id, start_ms, end_ms)`, `delete(session_id)`.
Ingest holds a `persist_audio` flag, default **on**, overridden by
`capture.save_audio` once Phase 4 lands (this is the corrected dependency —
Phase 3 does not require Phase 4).

### 3.2 `GET /segments/{id}/audio` (blocking #3)

1. Resolve segment → `(session_id, start_ms, end_ms)`; 404 if no frame log.
2. Mux `read_range(...)` into Ogg Opus, expanding gap records to silent frames,
   and cache at `<data>/audio/seg/<segment_id>.ogg`.
3. Serve the cached file with `web.FileResponse` — aiohttp gives `Range`,
   `206` and `ETag` for free.

Building the container on read means a muxer bug is recoverable (delete the
cache, fix, re-serve) rather than corrupting the write path. Open segments
serve what exists so far and are not cached.

Also stop lying on the event wire: `_event_to_wire`
(`http/routes/sessions.py:104-109`) currently hardcodes `codec:""`,
`sampleRateHz:0`, `byteCount:0`. Fill `"opus"`, `16000` and the real byte count
when the audio store has the session.

### 3.3 `GET /segments/{id}/waveform` (blocking #4)

Compute peaks during ingest — the decoded PCM hop is already in hand at
`pipeline.py:159`, so `max(abs(sample))` per 500 ms bucket is nearly free.
Persist as a fixed-stride binary array `<session_id>.peaks` (one `u8` per
bucket, bucket `i` = `[i*500, (i+1)*500)` ms of session time, gaps = 0). Same
indexing invariant as the frame log, so a segment's waveform is a slice, not a
scan.

```json
{ "schemaVersion": "v1", "bucketMs": 500, "peaks": [0.12, 0.55],
  "durationMs": 64000 }
```

The client resamples to its 34 bars. 404 when no audio.

Touched: new `media/audio.py`, new `media/ogg.py` (mux-on-read only),
`ingest/pipeline.py`, `http/routes/segments.py`, `http/routes/sessions.py`,
`http/app.py`, `scripts/run_gateway.py`, `sim/` (the sim must exercise the tap,
including loss), `tests/media/`, `tests/http/`, `tests/integration/`.

**Storage estimate:** speech-only Opus at ~2–4 KB/s, so roughly 10 MB per hour
*of speech*, plus ~6 bytes per silence run. A day of wear with two hours of
talking is ~20 MB. The earlier 115 MB/hour figure assumed continuous PCM and
does not apply.

---

## Phase 4 — Settings and device control (D2, D3)

### 4.1 `GET` / `PUT /settings` (significant #9)

New `settings.db`, table `settings(key TEXT PK, value TEXT /*JSON*/,
updated_at TEXT)`; new `settings/store.py` (`SettingsStore`: `get_all`, `put`),
SQLite + in-memory, `app["sense_settings_store"]`. Open KV in storage, a fixed
typed document on the wire:

```json
{ "schema_version": "v1",
  "capture": { "audio_enabled": true, "save_audio": true,
               "vision_enabled": false },
  "retention": { "audio_days": 30 } }
```

`PUT` accepts a full or partial document (pydantic, `extra="forbid"` → unknown
keys 400), merges, persists, returns the merged result.

- `capture.audio_enabled` — **device-level gate.** Desired state; drives §4.2.
- `capture.save_audio` — **server-level.** Controls the Phase 3 tap only.
- `retention.audio_days` — §5.3.

`audio_enabled=false` also gates ingest as a backstop, so the toggle is never
cosmetic even while the device is offline or ignoring commands.

**Note:** findings.md refers to "all three capture toggles" without naming
them, and one of the design's three was the wake-word toggle, which is cut (see
Out of scope). The third toggle's identity needs a look at the design file
before this ships.

### 4.2 Desired-state reconciliation (D2)

Firmware supports this: `start_audio` → `audio_gate_set(false)`, `stop_audio` →
`audio_gate_set(true)` (`executor.c:26-33`), and audio is always-on at boot
(`audio_gate.c:3`), so `start_audio` is really *resume*.

Persist last-known device state (`device_state.audio_gate` in `settings.db`,
updated on `command_ack`). On each `hello`, and on any `PUT /settings` that
changes `audio_enabled`, compare desired against last-known and **issue a
command only on mismatch**.

This guard is load-bearing: `CommandDispatcher` dedups on `idempotency_key`
only while a command is unacked (`commands/dispatcher.py`), so a
reconcile-on-every-reconnect loop without it would issue duplicate
`start_audio` commands on every flap.

### 4.3 `POST /commands` (blocking #5, D3)

```json
{ "type": "start_audio", "session_id": null, "params": {},
  "idempotency_key": "…" }
```

Flow — reuse the agent's chain rather than building a parallel one:

1. Validate against `ALLOWLIST` + `_TYPE_SCHEMAS`
   (`agent/validator_command.py:44-108`; `start_audio`/`stop_audio` take no
   params).
2. Apply `StrictCommandGuardrails` with the capability snapshot, as
   `Planner._dispatch_command` does (`agent/planner.py:335-368`).
3. Build the `Command` (id generator, clock, 5-min TTL, pass-through
   idempotency key) and `dispatcher.issue(...)` — signs Ed25519, dedups,
   persists.

Extract this into `commands/issue.py: validate_and_issue(...)` so the planner
and the route cannot drift.

**Unbound delivery (D3):** `session_id` omitted or empty means "the device,
whenever connected". `GatewayCore._pending_commands` (`gateway/core.py:284-291`)
changes its filter from `== self._session_id` to
`in ("", self._session_id)`, and stamps the live session id into the outgoing
`CommandMessage`. The signed payload keeps the empty value — which is safe
because firmware never reads `session_id`.

Responses: 201 with the command wire shape + `status: "PENDING"`; validation
failure → 400 with the validator's reason; guardrail refusal → 403; idempotent
replay → 200 with the original command.

**Security note, not in scope but worth filing:** firmware verifies the Ed25519
signature but never checks `expires_at` or `issued_at` (`commands.c:67-105`,
`executor_core.c:32-76`). A captured signed command replays successfully once
it falls out of the 16-entry dedupe ring (`commands.c:19,38-50`). The 5-minute
TTL the server sets is currently advisory.

Touched: new `settings/store.py`, new `http/routes/settings.py`, new
`commands/issue.py`, `http/routes/commands.py`, `agent/planner.py` (refactor),
`gateway/core.py`, `ingest/pipeline.py`, `http/app.py`,
`scripts/run_gateway.py`, tests.

---

## Phase 5 — Device status, deletion, retention

### 5.1 `GET /device/status` (significant #10)

**Stage A (this spec):** expose the `CapabilityProvider`
(`contracts/types.py:290-300`) over HTTP.

```json
{ "schema_version": "v1", "source": "static",
  "battery_pct": 0.45, "storage_free_bytes": null,
  "firmware_version": null,
  "recording": false, "relay_connected": true,
  "microphone_available": true, "camera_available": false,
  "last_packet_at": "2026-08-08T09:14:22Z", "last_packet_age_s": 4,
  "last_transcript_at": "2026-08-08T09:11:03Z" }
```

`source` is `"static"` or `"device"`, and it is how the client knows the
device-reported numbers are not measured. `storage_free_bytes` and
`firmware_version` are `null` until Stage B.

### Connection freshness — the fields that are actually true (D9)

The bottom half of that payload needs no firmware and is genuinely measured, so
the Settings header should lead with it and treat battery as secondary.

| field | source |
|---|---|
| `relay_connected` | a live gateway connection exists |
| `recording` | `SessionLifecycle` reports an active session |
| `last_packet_at` / `last_packet_age_s` | wall clock stamped on every inbound audio packet |
| `last_transcript_at` | most recent transcript event, from `SessionIndex` |

**Why `last_packet_at` is a real heartbeat, not a speech detector.** Firmware
keeps emitting `C6_GAP_MARKER` packets while VAD suppresses silence
(`opensapien_sensor.c:82`, `ble_drain.c:88`), so packets arrive continuously
whether or not anyone is talking — the bring-up log records exactly this case,
gap-marker packets flowing with no transcripts
(`docs/bring-up/2026-07-27-real-device-bringup.md:221`). A stale
`last_packet_at` therefore means the device stopped talking to us, which is the
failure worth surfacing. `last_transcript_at` is the separate signal, and the
pair distinguishes the three states the user cares about: alive and hearing
speech, alive in a quiet room, and gone.

**Implementation.** New `gateway/liveness.py` holding a small mutable
`DeviceLiveness` (last packet wall-clock + monotonic, last connection open and
close), injected as `app["sense_liveness"]`. Written from
`GatewayCore.on_audio`, which is the single choke point for every inbound audio
frame (`gateway/adapter.py:45-51`), and from the adapter's connection open and
`finally` blocks. Age is computed from the monotonic stamp so a system clock
step cannot produce a negative or absurd age; the wall clock is only for
display. Read-only from the route, `null` on every field when no packet has
ever arrived.

Both `relay_connected` and `recording` depend on Phase 0.1 to be accurate —
without the deregister fix, a dropped session reports as still recording
forever.

**`battery_pct` is a hardcoded 45 % placeholder** so the Settings header has
something to render before telemetry exists. Two constraints on it:

- It lives in the route as `_PLACEHOLDER_BATTERY_PCT = 0.45`, **not** in
  `ConstantCapabilityProvider`. The provider's `battery_pct = 1.0`
  (`contracts/types.py:290`) is what `StrictCommandGuardrails` reads for its
  refuse-below floors (`_BATTERY_MIN_FOR_LONG_OP = 0.10`,
  `_BATTERY_MIN_FOR_QUICK_OP = 0.05`, `guardrails_command.py:44-45`). 45 % clears
  both, so moving it would be harmless *today* — but it would couple a display
  placeholder to command admission, and a future placeholder below 10 % would
  silently start refusing `request_buffer`. Keep the fake number on the
  presentation surface only.
- Ship it behind `source: "static"` and have the client render it visibly
  provisional (greyed, or with a "not reported" affordance). A plausible-looking
  45 % is worse than a blank if a user ever acts on it — the failure mode is
  someone trusting a half-full battery on a device that is actually dead.

When Stage B lands, the placeholder is deleted rather than kept as a fallback:
`source: "device"` with a real value, or `null`.

**Be clear about what this does not do:** Stage A does *not* deliver the
battery readout findings.md #10 asks for — it delivers a placeholder shaped
like one. No telemetry exists: the inbound protocol union is exactly
`Hello | Bye | CommandAck` (`protocol/messages.py:26-53`) with `extra="forbid"`,
and firmware sends no device state.

**Stage B (parked, D9):** a real battery readout needs four layers that do not
exist today — battery voltage reaching a readable pin (a divider or an I²C
fuel gauge, which may be a board revision), firmware reading it and converting
voltage to a percentage against a discharge curve, the relay forwarding it, and
only then a `telemetry` inbound frame plus a `ReportedCapabilityProvider` on
the server. The server slice is roughly a day; the other three are the actual
cost. Nothing here is scheduled.

Before anyone schedules it, the question to answer is the hardware one: **can
the current board sense battery voltage at all?** If not, the decision is
already made. When Stage B does land, the placeholder is deleted rather than
demoted to a fallback — `source: "device"` with a real value, or `null`.

### 5.2 `DELETE /segments/{id}` (significant #11)

Cascading and idempotent: transcript events in range (`EventStore.delete_range`
— new), atoms in range (`AtomStore.delete_range` — new), their vectors
(`MemoryIndex.delete_atoms` — new), the cached segment Ogg,
`AudioStore.erase_range` (overwrite the range with a gap record, preserving the
D5 slot invariant), segment meta, and the index entry.

204 on success, 404 unknown, 409 if the segment is still open. Order matters:
SQLite rows first, files second, index entry last — a crash mid-delete must not
resurrect the segment on `rebuild_from_store`, and deleting events first
guarantees that.

### 5.3 Retention sweep (D8)

A periodic task deletes frame logs, peak arrays and cached segment Oggs for
sessions whose last event is older than `retention.audio_days` (default 30).
**Transcripts, atoms and vectors are not swept.** This is a deliberate tiered
policy, not an oversight: audio is the bulky and most sensitive artifact, the
derived text is the product. It should be stated plainly in the app's privacy
copy, because "the recording expires but the transcript does not" is not what a
user assumes by default.

### 5.4 `DELETE /devices/{id}` (significant #12) — deferred

findings.md flags the semantics as unresolved ("Forget this Sense": local
unpair or server purge). v1 ships local unpair only, no endpoint. A server-side
purge-all is a distinct and dangerous operation deserving its own confirmation
design.

---

## Explicitly out of scope

- **Wake-word toggle** — contradicts the architecture. There is no wake-word
  engine in firmware and the wearable is designed without one. Cut it from the
  design or redefine it honestly as server-side gating after transcription.
- **Speaker naming and Commands screens** — the server already supports both
  (`/speakers/*`, `/commands`); the gap is that the design dropped them. No
  server work either way, but someone should confirm the cut was deliberate.
- **Extractor kind taxonomy** — see §1.2.
- **Chat history persistence** — findings.md notes the design does not require
  it.
- **Firmware command TTL enforcement** — filed as a security note in §4.3.

---

## Open questions

None blocking. Two items are deliberately deferred rather than unresolved:
firmware telemetry (D9, §5.1) and the "forget this device" semantics (§5.4).

One thing to confirm against the design file before Phase 4 ships: findings.md
refers to "all three capture toggles" without naming them, and one of the three
was the wake-word toggle, which is cut. §4.1 defines `audio_enabled`,
`save_audio` and `vision_enabled`; the third may not be what the design
intended.

---

## Sequencing and dependencies

| Phase | Contents | Depends on | Unblocks |
|---|---|---|---|
| 0 | lifecycle + session-end fixes | — | 2, 5 |
| 1 | memory list, stats, `occurred_at` | — | Memories tab |
| 2 | segments, titles, search, memory counts | 0 | Home, Recordings, Detail header |
| 3 | frame log, `/audio`, `/waveform` | 2 | Recording Detail player |
| 4 | settings, reconciler, `POST /commands` | — (3 for `save_audio`) | Settings |
| 5 | device status, delete, retention | 0, 2, 3, 4 | Settings header, ⋮ menu |

Phases 1 and 4 are independent of the 0 → 2 → 3 spine and can run in parallel
with it. The earlier draft claimed Phase 3 was independent of the settings
work; that was wrong, and it is resolved by defaulting `persist_audio` to on so
Phase 3 stands alone and Phase 4 merely overrides it.

Each phase lands with: store-level unit tests (SQLite and in-memory parity),
HTTP tests per new or changed route including a 401 case and whole-dict wire
assertions, and an updated integration test wherever the phase touches the
gateway path. Phase 3 additionally needs a lossy-stream test through the sim
device asserting the D5 slot invariant, and a golden-file test for the
mux-on-read output.
