# Sense Firmware — P4a Device Command Executors (audio)

> **Scope:** the first half of the P4 "firmware executors" milestone. This spec
> delivers the device-side command **executor framework** plus three executors that
> run on existing audio hardware with no new components: `request_buffer`,
> `start_audio`, and `stop_audio`. The remaining executors — `capture_photo` and
> `record_video` — need a new `esp32-camera` driver + microSD capture path and have
> no BLE transport (video is end-of-day over WiFi, a separate future slice); they
> are deferred to **P4b**. The project-status doc already sequences this work as
> "request_buffer + start/stop_audio, then photo/video."
>
> **Tech stack / context:** ESP-IDF 5.1.6, NimBLE 1.6, XIAO ESP32S3 Sense
> (dual INMP441 I2S mics, OV2640 camera unused in this slice), core layout
> core-1 = audio (capture → VAD → Opus → ring), core-0 = BLE (drain ring → §C.6 →
> notify; handle commands). Firmware pins to v5.1.6 to dodge the NimBLE 1.7
> `xQueueSemaphoreTake uxItemSize == 0` assert; install via
> `scripts/install_idf_5.1.6.sh`.

**Status:** Frozen 2026-08-03 after a brainstormed design and two confirmed
edge-case decisions. Implementation begins against this doc after the
writing-plans step.

---

## 1. What exists today (the seams this slice fills)

- **`commands.c:65` `commands_handle`** runs **synchronously on the NimBLE host
  task (core 0)**. It verifies the 64-byte Ed25519 signature over the canonical
  payload JSON (libsodium), parses with cJSON, dedupes by `command_id` (16-deep
  ring, at-least-once: a dup re-acks but does not re-execute), calls
  `execute(type, root)`, then `ack(command_id)`.
- **`commands.c:58` `execute()`** is a stub that only logs the type. Its comment
  enumerates the intended types: `capture_photo / record_video / start_audio /
  stop_audio / play_audio / display_text / show_status / request_buffer`.
- **No executor subsystem exists.** There are exactly two application tasks
  (`audio` core 1, `drain` core 0), no FreeRTOS queues, no mutexes, no task
  notifications anywhere in `main/`. The ring buffer is lock-free
  single-producer (`audio_task`) / single-consumer (`drain_task`).
- **`audio_task` (`sense_sensor.c:49`)** is always-on at boot: each 20 ms it
  reads one stereo I2S frame, runs single-mic energy VAD on the primary channel,
  Opus-encodes voiced/hangover frames, and pushes into the 60 s PSRAM ring.
  There is no start/stop gate.
- **`drain_task` (`ble_drain.c:28`)** is the single owner of the monotonic
  `chunk_seq` counter and the `ble_link_notify_audio` call. It drains the ring in
  `C6_FRAMES_PER_CHUNK` (50) frame batches, applies VAD preroll on speech onset,
  packetizes into MTU-sized `C6_LIVE` §C.6 packets, and notifies the phone.
- **The `request_buffer` wire hooks already exist** but are unused on the device:
  `C6_MEMORY_CHUNK = 1` ptype and `C6_FLAG_LAST_OF_REQ = 0x02` are defined in
  `config.h` and parsed by the server (`audio_packet.py`). The server reassembler
  (`reassembler.py`) orders **any** ptype by `chunk_seq` and does not special-case
  memory chunks; `AudioPacket.is_last_of_request` flags the final packet of a
  retrospective replay. `RequestChunks` (§E, server→relay) is a **separate**
  server-initiated head-gap backfill and is out of scope here.
- **Ack semantics today:** the device sends the bare `command_id` bytes on the
  ACK characteristic; the relay wraps them as `§E CommandAck{command_id}`; the
  server's `dispatcher.ack()` marks the command "delivered/executed." The device
  **never** reports `EXECUTING`/`COMPLETED`/`FAILED`; those lifecycle states stay
  server-driven. **P4a does not change this** (see §6).
- **Server param validation** (`validator_command.py`) already enforces
  `record_video duration_s ∈ [1,30]`, `request_buffer seconds ∈ [1,60]`, and an
  allowlist of the 5 P2 types. Firmware validation in this slice is
  **defense-in-depth**, not the primary gate.

## 2. Architecture & module boundaries

Three new modules plus small edits to four existing files. The split keeps each
unit single-purpose and host-testable where possible, following the firmware's
established pattern (`c6_packet`/`vad` are portable C with host tests; `ble_*`
are ESP-only).

```
main/
  executor.c / executor.h        NEW  — dispatch table + per-type handlers
  executor_port.h                NEW  — host/ESP seam for deps the pure logic needs
  audio_gate.c / audio_gate.h    NEW  — the pause/resume flag read by audio_task
  commands.c                     EDIT — execute() → executor_submit()
  ble_drain.c / ble_drain.h      EDIT — drain task services a replay-request queue
  config.h                       EDIT — new constants (§7)
  sense_sensor.c                 EDIT — executor_init() + audio_gate wiring in app_main
test/
  test_executors.c               NEW  — host unit tests for the portable logic
```

**Responsibilities:**

- **`executor`** owns: parsing the cJSON `params` into a typed command struct,
  per-type param validation (mirroring the server `_TYPE_SCHEMAS`), the dispatch
  table mapping `type` → handler, and submitting a `cmd_request_t` to the
  executor queue. The **portable** parts (parse + validate + the
  `request_buffer` window math) compile on the host behind `executor_port.h`
  stubs; the queue/task/notify calls are ESP-only and compiled out for host tests.
- **`audio_gate`** owns: a single `volatile bool s_paused` plus
  `audio_gate_set(paused)` / `audio_gate_paused()` accessors. `audio_task`
  (core 1) reads it each frame; the executor task (core 0) writes it.
- **`ble_drain`** gains: a replay-request queue (SPSC, `replay_request_t`) it
  polls at the top of each loop iteration; when present it emits the last
  `seconds` of ring frames as `C6_MEMORY_CHUNK` packets using the **same**
  `chunk_seq` counter and MTU-sized packetization as live, flags the final
  packet `C6_FLAG_LAST_OF_REQ`, then resumes live draining.

No new GATT characteristics, no new UUIDs, no new `idf_component.yml`
dependencies.

## 3. Command data flow (end to end)

```
phone writes [64B sig][canonical JSON] to COMMAND char
        │  (NimBLE host task, core 0)
        ▼
commands_handle()                         [commands.c — shape unchanged]
  ├─ verify Ed25519 sig vs provisioned key   (fail → drop, NO ack)
  ├─ parse JSON, require command_id + type    (fail → drop, NO ack)
  ├─ dedup by command_id (16-deep ring)
  │     └─ already_seen → re-ack, do NOT re-execute
  ├─ execute() → executor_submit(type, params, command_id)
  │     ├─ portable: executor_parse_and_validate → cmd_request_t{type, status, ...}
  │     │        status ∈ {OK, BAD_PARAMS, UNKNOWN_TYPE} (host-testable; §9.1)
  │     ├─ xQueueSend(executor_queue, &req, 0-tick)  ← NON-blocking; always attempted
  │     └─ returns true on successful enqueue, false if queue full
  └─ ack(command_id)   ← ONLY if enqueue succeeded (see §6.1); BAD_PARAMS still acks
        │  (ACK char → relay §E CommandAck{command_id} → server dispatcher.ack)
        ▼
executor_task (core 0, new)               [executor.c — ESP-only]
  └─ xQueueReceive(executor_queue, &req, portMAX_DELAY)
       └─ if req.status != OK → log + drop (no work; §6.2)
       └─ else dispatch by req.type:
            start_audio    → audio_gate_set(false)
            stop_audio     → audio_gate_set(true)
            request_buffer → xQueueSend(drain_replay_queue, {req.seconds}, 0) (§6.10)
            capture_photo  → log "not implemented (P4b)" (no-op; see §6.9)
            record_video   → log "not implemented (P4b)" (no-op; see §6.9)
            (play_audio / display_text / show_status — not in server ALLOWLIST;
             executor classifies as UNKNOWN and logs; never issued autonomously)
```

`commands_handle` stays synchronous on the NimBLE task but now does only cheap
work: verify + parse + dedup + enqueue + ack. The executor queue send is
non-blocking (0-tick wait). The ack fires only after a successful enqueue.

## 4. `request_buffer` executor + drain replay

### 4.1 Executor side (instant, on the executor task)

Param validation already happened in the host-testable pure step inside
`executor_submit` (§3: `executor_parse_and_validate` runs on the NimBLE task
before enqueue, so the `cmd_request_t` on the queue carries an already-validated
`seconds`). The executor task handler therefore just forwards:

1. Build `replay_request_t { uint32_t seconds; }` from the validated
   `cmd_request_t` and `xQueueSend(drain_replay_queue, &rr, 0)`. Non-blocking;
   if the replay queue is full, log `replay queue full — dropped` (§6.2). The
   command already acked at the `commands_handle` layer (the executor-queue
   enqueue succeeded), so a dropped replay is a documented best-effort loss, not
   a protocol violation.

The validation itself (present, numeric, `REQ_BUFFER_MIN_SECONDS ≤ seconds ≤
REQ_BUFFER_MAX_SECONDS`, booleans rejected — mirroring the server's
`_is_number`) lives in `executor_parse_and_validate`, called from
`executor_submit`. Crucially, a validation failure does **not** gate the
executor-queue enqueue: the `cmd_request_t` carries a status (`OK`,
`BAD_PARAMS`, or `UNKNOWN_TYPE`), the enqueue is always attempted, and the ack
depends **only** on enqueue success (§6.1). The executor task handler then drops
a `BAD_PARAMS`/`UNKNOWN_TYPE` request with a log and does no work (§6.2). This
keeps the validation host-testable as a pure function (§9.1) and makes "authentic
command received" (→ ack) orthogonal to "params usable" (→ maybe no replay).

### 4.2 Drain side (the actual replay, on the existing drain task)

At the **top** of each drain-loop iteration, **before** the live-drain work,
drain pending replay requests back-to-back:

```c
replay_request_t rr;
while (xQueueReceive(drain_replay_queue, &rr, 0) == pdPASS) {
  drain_replay(rr.seconds);
}
```

`drain_replay(seconds)`:

1. **Snapshot the window.** `uint32_t write_idx = ring_buffer_write_index();`
   `uint32_t want = seconds * (1000 / FRAME_MS);`  (`seconds * 50`)
   `uint32_t N = want;`
   `if (N > RING_FRAMES) N = RING_FRAMES;`        (guard >60 s; impossible given bounds)
   `if (N > write_idx) N = write_idx;`            (early-boot: replay only what exists)
   `uint32_t start_idx = write_idx - N;`          (clamped ≥ 0 because N ≤ write_idx)
   Snapshot `write_idx` once here; read only frames `< write_idx` so the window
   is stable for the duration of the replay.
2. **Walk `start_idx … start_idx+N`** using the **same MTU-sized packetization
   the live path uses** (`ble_link_att_mtu()` → `max_payload = mtu - 3` →
   `budget = max_payload - C6_HEADER_LEN`; pack as many frames per packet as fit;
   one `chunk_seq` per packet), but with `ptype = C6_MEMORY_CHUNK`.
3. **`chunk_seq` is the same monotonic counter** the live path uses — `drain_replay`
   reads and increments the same `chunk_seq` local that `drain_task` owns. This is
   why the replay **must** run on the drain task: a single owner of that counter.
4. **Per-packet header fields:** `rel_ts_ms` = the original `rel_ts` of the
   packet's first frame (preserved by the ring); `vad_state` = that frame's own
   `vad_state` verbatim; `flags = 0` except on the final packet. `drain_replay` is
   a standalone function: it reuses only the live path's **MTU-packetization
   helper**, not the live preroll/`in_speech` logic. Preroll is a live-onset
   concern and has no role in a retrospective replay — the window already
   contains whatever was captured, frames are emitted as-is.
5. **Gap markers:** the live path suppresses `C6_GAP_MARKER` / `len == 0` frames
   (drops them from the body but still advances `chunk_seq`). The replay does the
   same: silence frames are not included in packet bodies. **If the entire window
   is silence**, emit a single empty `MEMORY_CHUNK` packet (`frame_count = 0`)
   with `C6_FLAG_LAST_OF_REQ` so the server sees a clean request boundary.
6. **Final packet:** set `flags |= C6_FLAG_LAST_OF_REQ` on the last packet of the
   replay (whether it carries audio or is the empty-boundary packet above).
7. **Overwritten-frame race:** `audio_task` (core 1) keeps writing the ring during
   the replay. The replay window is ≤ 60 s == the ring's retention, so the oldest
   replay frame is the one about to be overwritten. If `ring_buffer_get(idx, &f)`
   returns false for an in-range `idx` (frame overwritten mid-replay), emit a
   zero-frame gap-marker packet for that slot (preserves `chunk_seq` continuity;
   the server reassembler treats it as silence) and log once per replay at most.
8. **No subscriber:** `ble_link_notify_audio` returns `<0` if unsubscribed — same
   as live; the replay continues to completion so `LAST_OF_REQ` is emitted
   regardless. Ack semantics unchanged.
9. Return; the loop falls through to live draining.

### 4.3 Live-vs-replay ordering

Because `drain_replay` runs synchronously inside the drain task **before** the
live branch, live audio is naturally delayed for the replay's duration (the time
to notify `N` frames, well under a second for `N ≤ 3000`). No second task, no
flag, no interleaving, no second producer on the notify path. The ring keeps
being written by `audio_task` throughout; when live draining resumes, the live
cursor is behind `write_idx` and catches up as normal (the existing preroll logic
applies to the next voiced onset).

## 5. `start_audio` / `stop_audio` + `audio_gate`

`audio_gate` is deliberately tiny:

```c
// audio_gate.h
void  audio_gate_set(bool paused);    // writer: executor task, core 0
bool  audio_gate_paused(void);         // reader: audio_task, core 1
```

- Backed by `static volatile bool s_paused;`. **No mutex.** Rationale: one writer,
  one reader, and a torn read at worst causes one extra/missed 20 ms frame —
  which the gap-marker path already tolerates. Reads/writes of an aligned bool
  are single-instruction on Xtensa LX7. Documented in the header.
- **Default at boot = not paused** (audio always-on, byte-identical to today
  until a `stop_audio` command arrives). No init value change required beyond
  the static zero-initialization.
- **`audio_task` change** — insert immediately after
  `audio_capture_read_stereo()` returns `ESP_OK`, before the energy accumulators.
  The paused branch pushes a gap marker and **falls through to the existing
  per-frame tail** (`frames++; rel_ts_ms += FRAME_MS;` + the per-second log +
  stack-high-water block), so all monitoring stays identical and nothing is
  skipped except the energy accumulators, VAD, and Opus encode:

  ```c
  if (audio_gate_paused()) {
    // Drain DMA (read already happened) but capture no speech: push a gap
    // marker so the ring stays contiguous, then skip VAD + encode. Fall
    // through to the shared per-frame bookkeeping tail (frames++, rel_ts,
    // per-second log, stack high-water) — only the audio work is skipped.
    ring_buffer_push(C6_GAP_MARKER, rel_ts_ms, NULL, 0);
    gaps++;
    goto frame_tail;   // jump past the VAD/encode block to the common tail
  }
  // ...existing energy accumulators + VAD + Opus encode + ring push...
  // label `frame_tail:` sits just before `frames++; rel_ts_ms += FRAME_MS;`
  ```

  A `goto` to the common tail is the smallest, clearest way to share the
  bookkeeping without duplicating the per-second/stack-high-water logic. (If the
  implementer prefers, an equivalent `bool paused` flag that wraps only the
  encode block is equally acceptable — the binding requirement is: paused frames
  push a gap marker, skip VAD+encode, and still run the per-second + stack-high
  -water bookkeeping.)

  The I2S read still happens every 20 ms (drains DMA so it doesn't stall); VAD,
  Opus encode, and the energy accumulators are skipped. The per-second log
  naturally shows `voiced=0 opus_bytes/s=0` and `cal e_pri=0 e_ref=0 ratio=0`
  while paused. The ring stays contiguous (`chunk_seq` and `rel_ts` keep
  advancing via the gap markers the drainer emits), so a later `start_audio`
  resumes with no cursor break.

- **Idempotent:** `stop_audio` when already paused is a no-op (still acks); same
  for `start_audio`. The gate is a pure set-state, not a toggle.
- **No interaction with an in-flight replay** (§6.7): the gate affects future
  `audio_task` frames; the replay reads already-written ring frames.

## 6. Error handling & edge cases

1. **Queue-full on enqueue → no ack.** `executor_submit` returns `false` if
   `xQueueSend(executor_queue, …)` fails (queue full). `commands_handle` acks
   **only on successful submit**; on failure it logs `cmd queue full — dropped,
   no ack` and does **not** ack. The server re-issues (at-least-once). This keeps
   the ack honest ("I accepted it") rather than acking-then-dropping. **(Confirmed
   decision.)**
2. **Param validation failure (`BAD_PARAMS`) / unknown type (`UNKNOWN_TYPE`).**
   The command was signature-verified and dedup-remembered, so it cannot be
   treated as "didn't arrive." The `cmd_request_t` is enqueued to the executor
   queue (so §6.1 acks it — the device received an authentic command) **but**
   carries a `BAD_PARAMS`/`UNKNOWN_TYPE` status; the executor task handler logs
   `request_buffer bad params` / `unknown type` and does no work — no replay is
   forwarded to the drain queue. The server's guardrails should already have
   prevented out-of-range params (firmware validation is defense-in-depth).
   Operator-visible symptom: "I asked for the last 30 s but got nothing back" —
   driven by server-side validation, not firmware. (Distinct from §6.1: a full
   *executor* queue means the command was never accepted → no ack; a `BAD_PARAMS`
   command was accepted → ack, just no effect.)
3. **Replay window > ring contents** (early boot, <60 s elapsed). Cap `N` to
   `write_idx` and replay what exists; still emit the `LAST_OF_REQ` final packet.
   Log `replay capped: requested %u s, have %u frames`.
4. **Replay window > RING_FRAMES** (>60 s). Cap `N` to `RING_FRAMES`. Impossible
   given `seconds ≤ 60 == RING_SECONDS`, but guarded.
5. **Frames overwritten mid-replay.** See §4.2.7 — emit a gap-marker packet for
   the slot, log once.
6. **No BLE subscriber.** Replay completes regardless; `LAST_OF_REQ` emitted.
7. **`start_audio`/`stop_audio` during a replay.** No interaction (§5).
8. **Dedup of a re-delivered `request_buffer`.** A duplicate `command_id`
   re-acks but does not re-execute (existing `commands_handle` behavior), so a
   re-delivered replay command does not fire twice. Correct.
9. **`capture_photo`/`record_video` in P4a.** The dispatch table has entries for
   them (so the dispatch is complete and forward-compatible) but they are
   no-ops: the executor logs `capture_photo not implemented (P4b)` /
   `record_video not implemented (P4b)` and does nothing else. The command still
   acks (valid, authentic, received). These two types **are** in the server
   `ALLOWLIST` today, so the server *could* issue them autonomously; in P4a the
   device acks no-op. **(Confirmed decision: leave the server allowlist alone;
   document this. Do not temporarily remove photo/video from the server
   allowlist.)** The operator should not issue photo/video from the UI until P4b.
   `play_audio`/`display_text`/`show_status` are not in the server `ALLOWLIST`
   (never issued autonomously); the executor classifies them as `UNKNOWN` and
   logs, never reaching a handler.
10. **Drain replay queue full.** Distinct from §6.1 (executor queue). The
    command already acked when it entered the executor queue (§6.1 satisfied). If
    the executor task handler's `xQueueSend(drain_replay_queue, …, 0)` fails
    (replay queue full), it logs `replay queue full — dropped` and moves on. The
    command is lost at best-effort; no second ack, no replay. Under steady state
    the drain task drains this queue to empty every loop iteration, so it cannot
    backlog unless replays arrive faster than the drain task services them
    (depth 4 is generous; back-to-back replays serialize).

## 7. Config constants (`config.h`)

```c
/* ---- §D command executors (P4a) ---- */
#define EXECUTOR_TASK_STACK      4096   /* core-0; cJSON parse + queue send only */
#define EXECUTOR_TASK_PRIO       5      /* same as the other app tasks */
#define EXECUTOR_TASK_CORE       0      /* with the radio/drain, opposite audio */
#define EXECUTOR_QUEUE_DEPTH     8      /* SPSC; 8 in-flight commands is generous */
#define DRAIN_REPLAY_QUEUE_DEPTH 4      /* SPSC; replays serialize in the drain task */

/* request_buffer bounds — mirror server _TYPE_SCHEMAS (defense-in-depth) */
#define REQ_BUFFER_MIN_SECONDS 1
#define REQ_BUFFER_MAX_SECONDS 60
```

No new GATT UUIDs, no new characteristics, no new `idf_component.yml` deps.

## 8. Concurrency & memory model

| shared state | writer | reader | sync |
|---|---|---|---|
| `executor_queue` | NimBLE task (core 0) | executor task (core 0) | FreeRTOS queue (SPSC) |
| `drain_replay_queue` | executor task (core 0) | drain task (core 0) | FreeRTOS queue (SPSC) |
| `s_paused` (audio_gate) | executor task (core 0) | audio_task (core 1) | volatile bool, SPSC, tolerant |
| monotonic `chunk_seq` | drain task | drain task | not shared — single owner |
| ring buffer | audio_task (push) | drain task (read) | existing lock-free SPSC |

- All three FreeRTOS queues are SPSC — the safest queue usage. **No mutexes
  added.** The ring buffer's existing single-consumer invariant is preserved: the
  drain task reading for a replay is the same single consumer, just reading a
  different index range.
- **Core placement:** executor task on core 0 (with radio/drain, opposite the
  audio encode on core 1). It does no CPU-heavy work in P4a (param parse + queue
  send), so it won't contend meaningfully with NimBLE/drain. Priority 5 — not
  latency-critical, and it never blocks the radio since `commands_handle`
  already offloaded to it.
- **Stack:** executor task 4096 B is plenty (cJSON parse is shallow; no deep
  calls in P4a). Log `uxTaskGetStackHighWaterMark` periodically (right-size
  later), matching the other tasks' pattern.

## 9. Testing

### 9.1 Host unit tests (`test/test_executors.c` — portable logic only)

Compile under the existing host harness (no `freertos/` or `esp_*` headers), via
`executor_port.h` stubs — mirroring `test_c6_packet.c` / `test_vad.c`.

- **Param parsing & validation:**
  - `request_buffer` with valid `seconds` (1, 30, 60); missing `seconds`;
    non-numeric; out-of-range (0, 61, -1, float); boolean `seconds` (rejected);
    extra unknown params.
  - `start_audio` / `stop_audio` / `capture_photo` with no params (valid shape);
    with unexpected params (rejected).
  - `record_video` `duration_s` bounds (1, 30, 0, 31) — even though P4a no-ops
    photo/video, the parse/validate must still classify them so the dispatch
    table is complete and P4b just swaps the handler.
- **Dispatch table:** every `CommandType` string → correct handler enum;
  `play_audio` / `display_text` / `show_status` / unknown string → `UNKNOWN`
  (classified, not dispatched).
- **`request_buffer` window math** as a pure function
  `replay_window(seconds, write_idx) -> {start_idx, N}`:
  - nominal (`seconds=5`, `write_idx=10000` → `start=9750, N=250`);
  - early-boot cap (`seconds=60`, `write_idx=200` → `start=0, N=200`);
  - over-60s cap (`seconds=60`, `write_idx=100000` → `start=97000, N=3000 =
    RING_FRAMES`);
  - boundary (`seconds=1` → `N=50`; `seconds=60` → `N=3000`).

### 9.2 ESP-only wiring (build + on-device smoke, no host test)

- `idf.py build` clean.
- **On-device smoke (bring-up runbook, operator-driven):**
  - `stop_audio` → per-second log shows `voiced=0 (paused) gap=…`; the gateway
    sees only empty live packets (no transcripts while paused).
  - `start_audio` → speech returns; transcripts resume.
  - `request_buffer seconds=5` → monitor shows `MEMORY_CHUNK` packets and a
    final `LAST_OF_REQ` packet; the gateway log shows a retrospective
    transcript segment appear after the live stream.
  - `request_buffer seconds=60` on a freshly-booted device (<60 s elapsed) →
    capped replay, `LAST_OF_REQ` still emitted, log `replay capped`.

### 9.3 Server regression

No server changes in P4a, so `pytest -q` stays green. Re-run as a sanity guard
only.

## 10. Out of scope (deferred)

- **`capture_photo` / `record_video` executors** — P4b (new `esp32-camera`
  component + microSD capture; EOD-over-WiFi retrieval is a further slice).
- **`play_audio` / `display_text` / `show_status` executors** — not in the
  server `ALLOWLIST`; no hardware (no speaker/display on the wearable).
- **`EXECUTING` / `COMPLETED` / `FAILED` lifecycle reporting from the device** —
  would require extending the ACK characteristic frame + `§E CommandAck` + the
  server dispatcher, a cross-layer change onto the not-yet-on-device-validated
  Android relay. Deferred until the relay is proven on hardware.
- **`request_chunks` (§E server→relay head-gap backfill) handling on the
  device/relay** — separate mechanism from `request_buffer`; still unhandled by
  the relay today (logged, not forwarded).
- **Replay of audio older than the 60 s ring** — not possible by design; the
  ring is the retrospective buffer.

## 11. Risks

- **First queue-based subsystem in the firmware.** No in-repo precedent, but
  standard ESP-IDF. Mitigation: SPSC usage only; the drain replay queue is
  drained to empty every loop iteration so it cannot backlog under steady state.
- **Replay/live `chunk_seq` sharing.** Safe because the drain task is the single
  owner; the spec makes the replay run *inside* the drain task to preserve that.
  A future temptation to move replay to the executor task would break the
  single-owner invariant — called out in §4.2.3.
- **On-device validation not yet done.** The NimBLE 1.6 assert smoke (bring-up
  Tier 2) is still pending; P4a adds a task + two queues but no new BLE traffic
  shape, so it should not affect the assert. The on-device smoke in §9.2 is the
  validation step.
- **Photo/video no-op ack in P4a.** A latent footgun: the server could issue
  them and the device silently does nothing. Mitigated by documenting it (§6.9)
  and the operator not issuing them until P4b; the alternative (churning the
  server allowlist) was explicitly rejected to keep P4a firmware-only.