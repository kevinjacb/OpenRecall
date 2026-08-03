# Firmware P4a Command Executors (audio) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the device-side command executor framework plus three audio executors (`request_buffer`, `start_audio`, `stop_audio`) on the XIAO ESP32S3 Sense firmware, with the pure logic host-tested and the wiring exercised by `idf.py build` + on-device smoke.

**Architecture:** `commands_handle` (NimBLE host task) offloads to a new executor task via a FreeRTOS queue; `start_audio`/`stop_audio` flip a `volatile bool` `audio_gate` read each frame by `audio_task`; `request_buffer` hands a replay request to the existing `drain_task`, which emits the last N seconds of ring frames as `C6_MEMORY_CHUNK` packets (final one flagged `C6_FLAG_LAST_OF_REQ`) using the same monotonic `chunk_seq` counter. The ack fires only on successful enqueue. `capture_photo`/`record_video` validate but no-op (P4b).

**Tech Stack:** ESP-IDF 5.1.6 / NimBLE 1.6 (XIAO ESP32S3 Sense), cJSON, libsodium, FreeRTOS, host-compiled C unit tests (no ESP toolchain) via `test/Makefile`.

## Global Constraints

- **IDF pin:** firmware is pinned to ESP-IDF v5.1.6 (NimBLE 1.6). Activate with `. ~/esp/esp-idf-v5.1.6/export.sh` in a fresh shell before any `idf.py` command. Do NOT bump to 6.0+ (the `driver` component name and NimBLE version are pinned for a reason — see `main/CMakeLists.txt`).
- **Host tests run with no ESP toolchain:** `cd firmware/sense_sensor/test && make`. Pure-C, compiled with `cc -std=c11 -Wall -Wextra -I../main`. Do not introduce `freertos/` or `esp_*` headers into any file compiled by the host test (`executor_core.c`, `audio_gate.c`).
- **Wire contract fidelity:** §C.6 packet layout is byte-frozen — mirror `server/src/sense_server/ingest/audio_packet.py` exactly. `C6_MEMORY_CHUNK = 1`, `C6_FLAG_LAST_OF_REQ = 0x02` (config.h:77-78). The server reassembler orders any ptype by `chunk_seq`; `request_buffer` replay MUST continue the same monotonic `chunk_seq` the live drain uses.
- **Server param contract (mirror exactly):** `record_video duration_s ∈ [1,30]`, `request_buffer seconds ∈ [1,60]`, no unknown param keys, booleans are NOT numbers (cJSON's `cJSON_IsNumber` is already false for `cJSON_True`/`cJSON_False` — use it). Source of truth: `server/src/sense_server/agent/validator_command.py` (`_TYPE_SCHEMAS`).
- **Ack rule:** the device acks a command **only if** it was successfully enqueued to the executor queue (queue-full → no ack → server re-issues). A `BAD_PARAMS`/`UNKNOWN_TYPE` command IS enqueued and acks, but the executor task does no work. See spec §6.1/§6.2.
- **Do not touch the live drain path's behavior:** the live `drain_task` loop must stay byte-identical except for the new replay-poll inserted at the very top of the loop. Do not refactor the live packetization into a shared helper in this slice (regression risk on the proven path); `drain_replay` duplicates the inner MTU-packet logic intentionally. This is a deliberate, noted deviation from spec §4.2.4's "helper" wording — the *algorithm* is reused, not shared code.
- **No new `idf_component.yml` deps.** No new GATT characteristics/UUIDs.
- **Commit style:** end commit messages with `Co-Authored-By: Claude <noreply@anthropic.com>`.

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `main/executor_core.h` | NEW (portable) | `cmd_request_t`, `executor_status_t`, `cmd_type_t`, `executor_parse_and_validate`, `replay_window`. No ESP headers. |
| `main/executor_core.c` | NEW (portable) | Implementations of the above. Host-compilable (cJSON + config.h only). |
| `main/executor.h` | NEW (ESP) | Includes `executor_core.h`; declares `executor_init`, `executor_submit` (ESP-only). |
| `main/executor.c` | NEW (ESP) | `executor_init` (queue + task), `executor_submit` (parse+validate + enqueue), `executor_task` (dispatch). |
| `main/audio_gate.h` | NEW (portable) | `audio_gate_set` / `audio_gate_paused`. |
| `main/audio_gate.c` | NEW (portable) | `static volatile bool s_paused` + accessors. |
| `main/ble_drain.h` | MODIFY | Add `replay_request_t`, `ble_drain_replay_queue()`. |
| `main/ble_drain.c` | MODIFY | Create replay queue; poll it at loop top; `drain_replay()`. |
| `main/commands.c` | MODIFY | `execute()` calls `executor_submit`, returns bool; `commands_handle` acks only on success. |
| `main/config.h` | MODIFY | Executor + replay-queue constants, `request_buffer` bounds. |
| `main/sense_sensor.c` | MODIFY | `audio_task` paused-branch; `app_main` calls `executor_init()`. |
| `main/CMakeLists.txt` | MODIFY | Register `executor.c`, `executor_core.c`, `audio_gate.c`. |
| `test/test_executors.c` | NEW | Host unit tests for `executor_core`. |
| `test/Makefile` | MODIFY | Add `exec` target (compiles cJSON + executor_core + test). |

**Header split rationale:** the spec listed `executor_port.h` as a host/ESP seam. The portable core needs no ESP deps (cJSON and config.h are both host-compilable), so a port header is unnecessary — the split is `executor_core.{h,c}` (portable, host-tested) + `executor.{h,c}` (ESP wiring). This is simpler than spec'd; noted here so the deviation is explicit.

---

### Task 1: Portable executor core + host tests

**Files:**
- Create: `firmware/sense_sensor/main/executor_core.h`
- Create: `firmware/sense_sensor/main/executor_core.c`
- Create: `firmware/sense_sensor/test/test_executors.c`
- Modify: `firmware/sense_sensor/test/Makefile`

**Interfaces:**
- Produces: `executor_core.h` with `cmd_request_t`, `executor_status_t`, `cmd_type_t`, `void executor_parse_and_validate(const char *type, const cJSON *params, cmd_request_t *out)`, `void replay_window(uint32_t seconds, uint32_t write_idx, uint32_t *start_idx, uint32_t *n)`. Consumes `config.h` (`FRAME_MS`, `RING_FRAMES`) and `cJSON.h`.

- [ ] **Step 1: Write the failing test**

Create `firmware/sense_sensor/test/test_executors.c`:

```c
/*
 * Host-side unit tests for the portable executor core (no ESP toolchain).
 *   cc -std=c11 -Wall -Wextra -I../main -I../managed_components/espressif__cjson/cJSON \
 *      ../managed_components/espressif__cjson/cJSON/cJSON.c ../main/executor_core.c \
 *      test_executors.c -o /tmp/exectest && /tmp/exectest
 *
 * Mirrors the server's _TYPE_SCHEMAS (validator_command.py): unknown-param
 * rejection, required-present, number-not-bool, bound checks.
 */
#include "executor_core.h"
#include "config.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* ---- executor_parse_and_validate ---- */

static cmd_request_t parse(const char *type, const char *params_json) {
  cJSON *p = params_json ? cJSON_Parse(params_json) : NULL;
  cmd_request_t req;
  executor_parse_and_validate(type, p, &req);
  if (p) cJSON_Delete(p);
  return req;
}

static void test_request_buffer(void) {
  printf("test_request_buffer\n");
  cmd_request_t r;
  r = parse("request_buffer", "{\"seconds\":1}");    check("min 1 ok",     r.status==EXEC_OK && r.type==CMD_REQUEST_BUFFER && r.seconds==1);
  r = parse("request_buffer", "{\"seconds\":30}");   check("30 ok",        r.status==EXEC_OK && r.seconds==30);
  r = parse("request_buffer", "{\"seconds\":60}");   check("max 60 ok",     r.status==EXEC_OK && r.seconds==60);
  r = parse("request_buffer", "{}");                 check("missing -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", NULL);                 check("null params -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":\"5\"}"); check("string -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":true}");  check("bool -> bad",   r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":0}");    check("below min -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":61}");   check("above max -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":5.0}"); check("float ok",     r.status==EXEC_OK && r.seconds==5);
  r = parse("request_buffer", "{\"seconds\":5,\"x\":1}"); check("unknown param -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_start_stop_audio(void) {
  printf("test_start_stop_audio\n");
  cmd_request_t r;
  r = parse("start_audio", NULL);   check("start_audio no params ok", r.status==EXEC_OK && r.type==CMD_START_AUDIO);
  r = parse("start_audio", "{}");    check("start_audio empty ok",      r.status==EXEC_OK);
  r = parse("start_audio", "{\"x\":1}"); check("start_audio unknown -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("stop_audio", NULL);     check("stop_audio no params ok",  r.status==EXEC_OK && r.type==CMD_STOP_AUDIO);
  r = parse("stop_audio", "{\"q\":1}");  check("stop_audio unknown -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_photo_video_classify(void) {
  printf("test_photo_video_classify\n");
  cmd_request_t r;
  /* photo: no params; OK (no-op in P4a, but classification must be right for P4b) */
  r = parse("capture_photo", NULL); check("photo ok", r.status==EXEC_OK && r.type==CMD_CAPTURE_PHOTO);
  r = parse("capture_photo", "{\"x\":1}"); check("photo unknown -> bad", r.status==EXEC_BAD_PARAMS);
  /* video: duration_s required + bounded */
  r = parse("record_video", "{\"duration_s\":1}");  check("video min ok", r.status==EXEC_OK && r.type==CMD_RECORD_VIDEO && r.duration_s==1);
  r = parse("record_video", "{\"duration_s\":30}"); check("video max ok", r.status==EXEC_OK && r.duration_s==30);
  r = parse("record_video", "{}");                  check("video missing -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":0}");   check("video below -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":31}");  check("video above -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":5,\"y\":1}"); check("video unknown -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_unknown_type(void) {
  printf("test_unknown_type\n");
  cmd_request_t r;
  r = parse("play_audio", NULL);    check("play_audio unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
  r = parse("display_text", NULL);  check("display_text unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
  r = parse("nonsense", NULL);      check("nonsense unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
}

/* ---- replay_window ---- */

static void test_replay_window(void) {
  printf("test_replay_window\n");
  uint32_t s, n;
  replay_window(5, 10000, &s, &n); check("nominal 5s", s==9750 && n==250);
  replay_window(1, 10000, &s, &n); check("1s -> 50 frames", n==50);
  replay_window(60, 10000, &s, &n); check("60s -> RING_FRAMES", n==RING_FRAMES && s==10000-RING_FRAMES);
  replay_window(60, 200, &s, &n);   check("early boot cap", n==200 && s==0);
  replay_window(1, 0, &s, &n);      check("zero write_idx", n==0 && s==0);
}

int main(void) {
  test_request_buffer();
  test_start_stop_audio();
  test_photo_video_classify();
  test_unknown_type();
  test_replay_window();
  if (failures) { printf("FAIL: %d check(s)\n", failures); return 1; }
  printf("ALL PASS\n");
  return 0;
}
```

- [ ] **Step 2: Add the Makefile target**

Edit `firmware/sense_sensor/test/Makefile`. Add the cJSON include to `CFLAGS`, add the `exec` target, and add `exec` to the `test` aggregate. The full new file:

```makefile
# Host-side contract tests for dependency-free firmware modules (no ESP toolchain).
# Run:  make
CC ?= cc
CFLAGS ?= -std=c11 -Wall -Wextra -I../main -I../managed_components/espressif__cjson/cJSON

CJSON_SRC = ../managed_components/espressif__cjson/cJSON/cJSON.c

test: c6 vad dsp provisioning exec

c6: /tmp/c6test
	/tmp/c6test

vad: /tmp/vadtest
	/tmp/vadtest

dsp: /tmp/dsptest
	/tmp/dsptest

provisioning: /tmp/provtest
	/tmp/provtest

exec: /tmp/exectest
	/tmp/exectest

/tmp/c6test: test_c6_packet.c ../main/c6_packet.c ../main/c6_packet.h ../main/config.h
	$(CC) $(CFLAGS) ../main/c6_packet.c test_c6_packet.c -o $@

/tmp/vadtest: test_vad.c ../main/vad.c ../main/vad.h ../main/config.h
	$(CC) $(CFLAGS) ../main/vad.c test_vad.c -o $@

/tmp/provtest: test_provisioning.c ../main/provisioning_core.c ../main/provisioning_core.h
	$(CC) $(CFLAGS) ../main/provisioning_core.c test_provisioning.c -o $@

/tmp/dsptest: test_mic_dsp.c ../main/mic_dsp.c ../main/mic_dsp.h ../main/config.h
	$(CC) $(CFLAGS) ../main/mic_dsp.c test_mic_dsp.c -o $@

/tmp/exectest: test_executors.c ../main/executor_core.c ../main/executor_core.h ../main/config.h $(CJSON_SRC)
	$(CC) $(CFLAGS) $(CJSON_SRC) ../main/executor_core.c test_executors.c -o $@

clean:
	rm -f /tmp/c6test /tmp/vadtest /tmp/dsptest /tmp/provtest /tmp/exectest

.PHONY: test c6 vad dsp provisioning exec clean
```

- [ ] **Step 3: Run the test to verify it fails**

```bash
cd firmware/sense_sensor/test && make exec
```
Expected: compile error — `executor_core.h` does not exist (`fatal error: 'executor_core.h' file not found`).

- [ ] **Step 4: Write `executor_core.h`**

Create `firmware/sense_sensor/main/executor_core.h`:

```c
/*
 * Portable executor core — command parse/validate + request_buffer window math.
 *
 * No ESP/FreeRTOS headers: compiles on the host (cJSON + config.h) so the
 * validation logic is unit-tested without the ESP toolchain (test/test_executors.c).
 * The ESP-only wiring (queue, task, dispatch) lives in executor.{h,c}.
 *
 * Validation mirrors the server's _TYPE_SCHEMAS (sense_server/agent/validator_command.py):
 * unknown param keys are rejected, required keys must be present, numbers must
 * not be booleans, and each field is bound-checked. This is defense-in-depth —
 * the server already validates before issuing; the device re-checks so a relay
 * bug or a future issuer can't drive a bad param into a handler.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  EXEC_OK = 0,
  EXEC_BAD_PARAMS = 1,
  EXEC_UNKNOWN_TYPE = 2,
} executor_status_t;

typedef enum {
  CMD_UNKNOWN = 0,
  CMD_START_AUDIO,
  CMD_STOP_AUDIO,
  CMD_REQUEST_BUFFER,
  CMD_CAPTURE_PHOTO,
  CMD_RECORD_VIDEO,
} cmd_type_t;

typedef struct {
  cmd_type_t        type;
  executor_status_t status;
  uint32_t          seconds;     /* request_buffer (validated)        */
  uint32_t          duration_s;  /* record_video   (validated; P4b)   */
} cmd_request_t;

/* Parse `type` + `params` (may be NULL) into `out`, setting out->status.
   On EXEC_OK, out->type + the relevant field are filled. */
void executor_parse_and_validate(const char *type, const cJSON *params, cmd_request_t *out);

/* Compute the request_buffer replay window: the last `seconds` of ring frames.
   N = min(seconds*50, RING_FRAMES, write_idx); *start_idx = write_idx - N. */
void replay_window(uint32_t seconds, uint32_t write_idx, uint32_t *start_idx, uint32_t *n);

#ifdef __cplusplus
}
#endif
```

- [ ] **Step 5: Write `executor_core.c`**

Create `firmware/sense_sensor/main/executor_core.c`:

```c
#include "executor_core.h"
#include "config.h"

/* True if `params` (may be NULL) contains only keys in `allowed` (NULL-terminated). */
static bool params_only_allowed(const cJSON *params, const char *const *allowed) {
  if (params == NULL || !cJSON_IsObject(params)) return true;  /* NULL/missing == empty */
  for (cJSON *child = params->child; child != NULL; child = child->next) {
    bool ok = false;
    for (const char *const *k = allowed; *k != NULL; k++) {
      if (strcmp(child->string, *k) == 0) { ok = true; break; }
    }
    if (!ok) return false;
  }
  return true;
}

/* Fetch a numeric (int/float, NOT bool) field. Returns false if missing/non-numeric/bool. */
static bool get_number(const cJSON *params, const char *key, double *out) {
  if (params == NULL) return false;
  const cJSON *node = cJSON_GetObjectItemCaseSensitive(params, key);
  if (node == NULL || !cJSON_IsNumber(node)) return false;  /* cJSON_IsNumber is false for True/False */
  *out = node->valuedouble;
  return true;
}

void executor_parse_and_validate(const char *type, const cJSON *params, cmd_request_t *out) {
  memset(out, 0, sizeof *out);
  out->status = EXEC_OK;

  if (strcmp(type, "start_audio") == 0) {
    out->type = CMD_START_AUDIO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "stop_audio") == 0) {
    out->type = CMD_STOP_AUDIO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "capture_photo") == 0) {
    out->type = CMD_CAPTURE_PHOTO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "record_video") == 0) {
    out->type = CMD_RECORD_VIDEO;
    static const char *const allowed[] = { "duration_s", NULL };
    if (!params_only_allowed(params, allowed)) { out->status = EXEC_BAD_PARAMS; return; }
    double d;
    if (!get_number(params, "duration_s", &d)) { out->status = EXEC_BAD_PARAMS; return; }
    if (d < 1.0 || d > 30.0) { out->status = EXEC_BAD_PARAMS; return; }
    out->duration_s = (uint32_t)d;
    return;
  }
  if (strcmp(type, "request_buffer") == 0) {
    out->type = CMD_REQUEST_BUFFER;
    static const char *const allowed[] = { "seconds", NULL };
    if (!params_only_allowed(params, allowed)) { out->status = EXEC_BAD_PARAMS; return; }
    double s;
    if (!get_number(params, "seconds", &s)) { out->status = EXEC_BAD_PARAMS; return; }
    if (s < (double)REQ_BUFFER_MIN_SECONDS || s > (double)REQ_BUFFER_MAX_SECONDS) {
      out->status = EXEC_BAD_PARAMS;
      return;
    }
    out->seconds = (uint32_t)s;
    return;
  }
  /* play_audio / display_text / show_status / anything else */
  out->type = CMD_UNKNOWN;
  out->status = EXEC_UNKNOWN_TYPE;
}

void replay_window(uint32_t seconds, uint32_t write_idx, uint32_t *start_idx, uint32_t *n) {
  uint32_t want = seconds * (1000u / FRAME_MS);   /* seconds * 50 */
  uint32_t N = want;
  if (N > RING_FRAMES) N = RING_FRAMES;
  if (N > write_idx)   N = write_idx;
  *start_idx = write_idx - N;
  *n = N;
}
```

- [ ] **Step 6: Run the test to verify it passes**

```bash
cd firmware/sense_sensor/test && make exec
```
Expected: `ALL PASS`.

- [ ] **Step 7: Run the full host test suite (regression)**

```bash
cd firmware/sense_sensor/test && make
```
Expected: c6, vad, dsp, provisioning, exec all pass.

- [ ] **Step 8: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add firmware/sense_sensor/main/executor_core.h firmware/sense_sensor/main/executor_core.c \
        firmware/sense_sensor/test/test_executors.c firmware/sense_sensor/test/Makefile
git commit -m "$(cat <<'EOF'
feat(firmware): portable executor core (parse/validate + replay window)

Host-tested command parse/validate mirroring server _TYPE_SCHEMAS
(unknown-param rejection, required-present, number-not-bool, bounds)
and the request_buffer replay-window math. No ESP deps — compiles on
the host via test/Makefile (links cJSON). Wiring lands in later tasks.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: audio_gate + executor framework + start_audio/stop_audio

**Files:**
- Create: `firmware/sense_sensor/main/audio_gate.h`, `firmware/sense_sensor/main/audio_gate.c`
- Create: `firmware/sense_sensor/main/executor.h`, `firmware/sense_sensor/main/executor.c`
- Modify: `firmware/sense_sensor/main/config.h` (add constants)
- Modify: `firmware/sense_sensor/main/commands.c` (execute() returns bool; ack on success)
- Modify: `firmware/sense_sensor/main/sense_sensor.c` (audio_task paused-branch; app_main executor_init)
- Modify: `firmware/sense_sensor/main/CMakeLists.txt` (register new sources)

**Interfaces:**
- Consumes: `executor_core.h` (Task 1); `audio_gate.h` (this task); `ble_drain.h` `ble_drain_replay_queue()` (NOT yet — that lands in Task 3; this task stubs the request_buffer handler to log "pending").
- Produces: `executor_init()` / `executor_submit(type, params) -> bool`; `audio_gate_set(bool)` / `audio_gate_paused()`. Later tasks rely on `executor.h` types.

- [ ] **Step 1: Add config constants**

Edit `firmware/sense_sensor/main/config.h`. Append before the closing `SERVER_ED25519_PUBKEY` block (after line 135 / the I2S section), a new section:

```c
/* ---- §D command executors (P4a) ---- */
#define EXECUTOR_TASK_STACK      4096   /* core-0; cJSON parse + queue send only */
#define EXECUTOR_TASK_PRIO       5      /* same as the other app tasks */
#define EXECUTOR_TASK_CORE       0      /* with radio/drain, opposite audio encode */
#define EXECUTOR_QUEUE_DEPTH     8      /* SPSC; 8 in-flight commands is generous */

/* request_buffer bounds — mirror server _TYPE_SCHEMAS (defense-in-depth) */
#define REQ_BUFFER_MIN_SECONDS 1
#define REQ_BUFFER_MAX_SECONDS 60
```

(DRAIN_REPLAY_QUEUE_DEPTH is added in Task 3 with the replay queue.)

- [ ] **Step 2: Write `audio_gate.h`**

Create `firmware/sense_sensor/main/audio_gate.h`:

```c
/*
 * Audio capture pause/resume gate (§D start_audio / stop_audio).
 *
 * One writer (executor task, core 0) + one reader (audio_task, core 1). Backed by
 * a single volatile bool — no mutex. A torn read at worst costs one extra/missed
 * 20 ms frame, which the gap-marker path already tolerates. Reads/writes of an
 * aligned bool are single-instruction on Xtensa LX7. Portable (no ESP headers).
 *
 * Default (static zero-init) = NOT paused: audio is always-on at boot, byte-
 * identical to today until a stop_audio command arrives.
 */
#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

void audio_gate_set(bool paused);   /* writer: executor task, core 0 */
bool audio_gate_paused(void);         /* reader: audio_task, core 1   */

#ifdef __cplusplus
}
#endif
```

- [ ] **Step 3: Write `audio_gate.c`**

Create `firmware/sense_sensor/main/audio_gate.c`:

```c
#include "audio_gate.h"

static volatile bool s_paused = false;

void audio_gate_set(bool paused) { s_paused = paused; }
bool audio_gate_paused(void)      { return s_paused; }
```

- [ ] **Step 4: Write `executor.h`**

Create `firmware/sense_sensor/main/executor.h`:

```c
/*
 * §D command executor — ESP-only wiring (queue + task + dispatch).
 *
 * commands_handle (NimBLE host task) calls executor_submit, which parse/validates
 * (executor_core.c, host-tested) and enqueues a cmd_request_t to a FreeRTOS queue.
 * The executor task dequeues and dispatches to per-type handlers. executor_submit
 * returns true only on successful enqueue — commands_handle acks only then, so a
 * full queue is an honest "not accepted" (server re-issues) rather than a lost ack.
 */
#pragma once

#include "esp_err.h"
#include <stdbool.h>

#include "executor_core.h"   // cJSON via executor_core.h
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Create the executor queue + task. Call once from app_main after commands_init. */
esp_err_t executor_init(void);

/* Parse+validate then enqueue. Returns true on successful enqueue (ack),
   false if the queue is full (no ack). Runs on the NimBLE host task. */
bool executor_submit(const char *type, const cJSON *params);

#ifdef __cplusplus
}
#endif
```

- [ ] **Step 5: Write `executor.c`**

Create `firmware/sense_sensor/main/executor.c`:

```c
#include "executor.h"

#include "audio_gate.h"
#include "ble_drain.h"
#include "config.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "exec";

static QueueHandle_t s_queue;

static void executor_task(void *arg) {
  (void)arg;
  static cmd_request_t req;
  for (;;) {
    if (xQueueReceive(s_queue, &req, portMAX_DELAY) != pdPASS) continue;
    if (req.status != EXEC_OK) {
      ESP_LOGW(TAG, "drop status=%d type=%d (no work)", req.status, req.type);
      continue;
    }
    switch (req.type) {
      case CMD_START_AUDIO:
        audio_gate_set(false);
        ESP_LOGI(TAG, "start_audio");
        break;
      case CMD_STOP_AUDIO:
        audio_gate_set(true);
        ESP_LOGI(TAG, "stop_audio");
        break;
      case CMD_REQUEST_BUFFER: {
        QueueHandle_t rq = ble_drain_replay_queue();
        if (rq == NULL) {
          ESP_LOGW(TAG, "request_buffer: drain replay queue not ready — dropped");
          break;
        }
        replay_request_t rr = { .seconds = req.seconds };
        if (xQueueSend(rq, &rr, 0) != pdPASS) {
          ESP_LOGW(TAG, "replay queue full — dropped seconds=%u", (unsigned)req.seconds);
        }
        break;
      }
      case CMD_CAPTURE_PHOTO:
        ESP_LOGI(TAG, "capture_photo not implemented (P4b)");
        break;
      case CMD_RECORD_VIDEO:
        ESP_LOGI(TAG, "record_video not implemented (P4b) dur=%u", (unsigned)req.duration_s);
        break;
      default:
        ESP_LOGW(TAG, "unknown cmd type=%d", req.type);
        break;
    }
  }
}

esp_err_t executor_init(void) {
  s_queue = xQueueCreate(EXECUTOR_QUEUE_DEPTH, sizeof(cmd_request_t));
  if (s_queue == NULL) {
    ESP_LOGE(TAG, "executor queue create failed");
    return ESP_FAIL;
  }
  BaseType_t ok = xTaskCreatePinnedToCore(executor_task, "executor",
                                          EXECUTOR_TASK_STACK, NULL,
                                          EXECUTOR_TASK_PRIO, NULL, EXECUTOR_TASK_CORE);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "executor task create failed");
    return ESP_FAIL;
  }
  ESP_LOGI(TAG, "executor up: queue=%d task_stack=%d core=%d",
           EXECUTOR_QUEUE_DEPTH, EXECUTOR_TASK_STACK, EXECUTOR_TASK_CORE);
  return ESP_OK;
}

bool executor_submit(const char *type, const cJSON *params) {
  static cmd_request_t req;
  executor_parse_and_validate(type, params, &req);
  if (xQueueSend(s_queue, &req, 0) != pdPASS) {
    ESP_LOGW(TAG, "executor queue full — dropped type=%s", type);
    return false;
  }
  return true;
}
```

Note: `ble_drain_replay_queue()` and `replay_request_t` are declared in `ble_drain.h` in Task 3. Until then this file won't compile against the current `ble_drain.h` — that's expected; Task 3 adds them. **Do not build until Task 3 Step 3.** (The request_buffer handler is written here so Task 3 only touches `ble_drain`.)

- [ ] **Step 6: Modify `commands.c` — `execute()` returns bool, ack on success**

Edit `firmware/sense_sensor/main/commands.c`. Add the include and change `execute()` + the dispatch site. Replace the existing `execute()` and the `else { remember(...); execute(...); ack(...); }` block.

Add near the top includes (after `#include "commands.h"`):
```c
#include "executor.h"
```

Replace the `execute` function (lines ~57-63):
```c
// Dispatch a verified, deduped command to the executor. Returns true if the
// command was accepted (enqueued -> ack); false if the executor queue was full
// (no ack -> server re-issues). BAD_PARAMS/UNKNOWN_TYPE are enqueued (and acked)
// but the executor task does no work — see spec §6.1/§6.2.
static bool execute(const char *type, const cJSON *root) {
  const cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
  return executor_submit(type, params);
}
```

Replace the dispatch block (the `else { remember(...); execute(...); ack(...); }` at lines ~94-98):
```c
  } else {
    remember(id->valuestring);
    if (execute(type->valuestring, root)) {
      ack(id->valuestring);   // accepted -> ack (at-least-once)
    } else {
      ESP_LOGW(TAG, "executor queue full — no ack for %s", id->valuestring);
    }
  }
```

- [ ] **Step 7: Modify `sense_sensor.c` — audio_task paused-branch**

Edit `firmware/sense_sensor/main/audio_task`. Add the include with the others at the top:
```c
#include "audio_gate.h"
```

In `audio_task`, insert a paused branch immediately after the `audio_capture_read_stereo` block (after the `if (audio_capture_read_stereo(pri, ref) != ESP_OK) { continue; }` and BEFORE the `for (int i = 0; ...)` energy accumulator loop). Add a `frame_tail:` label just before `frames++; rel_ts_ms += FRAME_MS;`. Concretely:

After:
```c
    if (audio_capture_read_stereo(pri, ref) != ESP_OK) {
      continue;  // DMA not ready yet; retry next tick
    }
```
Insert:
```c
    if (audio_gate_paused()) {
      // stop_audio: drain DMA (read above) but capture no speech. Push a gap
      // marker so the ring stays contiguous (chunk_seq + rel_ts keep advancing
      // via the drainer's empty packets). Skip energy/VAD/encode; fall through to
      // the shared per-frame tail so monitoring (per-second log, stack high-water)
      // stays identical to the live path.
      ring_buffer_push(C6_GAP_MARKER, rel_ts_ms, NULL, 0);
      gaps++;
      goto frame_tail;
    }
```

Then add the label before the existing `frames++; rel_ts_ms += FRAME_MS;` line (currently around line 110-111). Change:
```c
    frames++;
    rel_ts_ms += FRAME_MS;
```
to:
```c
  frame_tail:
    frames++;
    rel_ts_ms += FRAME_MS;
```
(The label must be followed by a statement — `frames++;` is that statement. The existing per-second log and stack-high-water blocks after it run unchanged for both paused and live frames. While paused, `voiced`/`total`/`acc_pri`/`acc_ref` are untouched, so the per-second log naturally shows `voiced=0 opus_bytes/s=0` and `cal e_pri=0 e_ref=0 ratio=0`.)

- [ ] **Step 8: Modify `sense_sensor.c` — app_main wiring**

Edit `app_main`. Add `#include "executor.h"` at the top. After the `commands_init(...)` block (around line 162-164) and before `xTaskCreatePinnedToCore(audio_task...)`, add:
```c
  if (executor_init() != ESP_OK) {
    ESP_LOGE(TAG, "executor_init failed");
  }
```
Keep the existing order: `commands_init` → `executor_init` → `xTaskCreatePinnedToCore(audio_task…)` → `ble_link_start(commands_handle)` → `ble_drain_start()`. The executor task is up before BLE advertising accepts commands.

- [ ] **Step 9: Register sources in CMakeLists.txt**

Edit `firmware/sense_sensor/main/CMakeLists.txt`. Add the three new sources to the `SRCS` list (alphabetical-ish, alongside the others):
```cmake
    SRCS
        "sense_sensor.c"
        "c6_packet.c"
        "audio_capture.c"
        "vad.c"
        "mic_dsp.c"
        "ring_buffer.c"
        "opus_stream.c"
        "ble_link.c"
        "ble_drain.c"
        "commands.c"
        "executor.c"
        "executor_core.c"
        "audio_gate.c"
        "provisioning.c"
        "provisioning_core.c"
```
(`executor.c` uses `freertos`/`esp_log` which are already available via the default REQUIRES; no new REQUIRES needed — `freertos` comes with the IDF component. If the build complains `freertos/queue.h` not found, add `freertos` to REQUIRES, but the existing `commands.c` already pulls libsodium/cjson transitively and the IDF exposes FreeRTOS headers to all components by default.)

- [ ] **Step 10: Commit (build deferred to Task 3)**

The ESP sources won't compile until Task 3 adds `ble_drain_replay_queue()` / `replay_request_t`. Commit the source now; build in Task 3.

```bash
cd /Users/kevin/Projects/Sense
git add firmware/sense_sensor/main/audio_gate.h firmware/sense_sensor/main/audio_gate.c \
        firmware/sense_sensor/main/executor.h firmware/sense_sensor/main/executor.c \
        firmware/sense_sensor/main/config.h firmware/sense_sensor/main/commands.c \
        firmware/sense_sensor/main/sense_sensor.c firmware/sense_sensor/main/CMakeLists.txt
git commit -m "$(cat <<'EOF'
feat(firmware): executor framework + audio_gate + start/stop_audio

Queue + executor task off the NimBLE radio task; start_audio/stop_audio
flip a volatile-bool audio_gate read each frame by audio_task (pause
encode, keep reading I2S, push gap markers -> ring stays contiguous).
commands_handle acks only on successful enqueue. request_buffer handler
is wired but its drain replay queue lands in the next task (build now).

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: request_buffer drain replay

**Files:**
- Modify: `firmware/sense_sensor/main/ble_drain.h` (add `replay_request_t`, `ble_drain_replay_queue()`)
- Modify: `firmware/sense_sensor/main/ble_drain.c` (create queue, poll at loop top, `drain_replay()`)
- Modify: `firmware/sense_sensor/main/config.h` (add `DRAIN_REPLAY_QUEUE_DEPTH`)

**Interfaces:**
- Consumes: `executor.c` (Task 2) calls `ble_drain_replay_queue()`; `replay_window()` from `executor_core.h` (Task 1) is NOT used here — the window math is inlined in `drain_replay` using the same `min(seconds*50, RING_FRAMES, write_idx)` logic (kept local so `drain_replay` is self-contained and the host-tested `replay_window()` stays a pure reference; they MUST agree — see Step 5's cross-check note).
- Produces: a working `request_buffer` path end-to-end (executor → drain replay queue → `C6_MEMORY_CHUNK` packets + final `C6_FLAG_LAST_OF_REQ`).

- [ ] **Step 1: Add the replay-queue depth to config.h**

Edit `firmware/sense_sensor/main/config.h`. In the P4a constants block added in Task 2, add:
```c
#define DRAIN_REPLAY_QUEUE_DEPTH 4      /* SPSC; replays serialize in the drain task */
```

- [ ] **Step 2: Extend `ble_drain.h`**

Edit `firmware/sense_sensor/main/ble_drain.h`. Add the FreeRTOS queue include and the accessor. After the existing `#include` block add:
```c
#include "freertos/queue.h"
```
Before the closing `#ifdef __cplusplus` guard, add:
```c
/* A request_buffer replay request, sent by the executor task and serviced by the
   drain task (the single owner of chunk_seq + ble_link_notify_audio). */
typedef struct {
  uint32_t seconds;   /* validated 1..60 */
} replay_request_t;

/* The drain task's replay queue. Created by ble_drain_start(); NULL before that.
   The executor task sends replay_request_t items here. */
QueueHandle_t ble_drain_replay_queue(void);
```

- [ ] **Step 3: Implement the replay queue + polling + `drain_replay` in `ble_drain.c`**

Edit `firmware/sense_sensor/main/ble_drain.c`. This is the largest edit; do it in three sub-parts.

**(a) Add file-static queue handle + accessor.** Near the top after `static const char *TAG = "drain";` add:
```c
static QueueHandle_t s_replay_queue;

QueueHandle_t ble_drain_replay_queue(void) { return s_replay_queue; }
```

**(b) Add `drain_replay` before `drain_task`.** Insert this function above `static void drain_task(void *arg)`:

```c
/* Emit the last `seconds` of ring frames as C6_MEMORY_CHUNK packets, continuing the
 * same monotonic chunk_seq the live path uses, with C6_FLAG_LAST_OF_REQ on the final
 * packet. Single owner of chunk_seq + notify => runs here on the drain task.
 *
 * The packetization mirrors the live path's MTU-sizing but is self-contained (no
 * shared helper) to avoid touching the proven live drain loop. The window math
 * agrees with replay_window() in executor_core.c (host-tested). */
static void drain_replay(uint32_t seconds, uint32_t *chunk_seq, uint8_t *pkt, size_t pkt_cap) {
  uint32_t write_idx = ring_buffer_write_index();
  uint32_t want = seconds * (1000u / FRAME_MS);   /* seconds * 50 */
  uint32_t N = want;
  if (N > RING_FRAMES) N = RING_FRAMES;
  if (N > write_idx)   N = write_idx;
  uint32_t start_idx = write_idx - N;
  if (want != N) {
    ESP_LOGI(TAG, "replay capped: requested %u s, have %u frames", (unsigned)seconds, (unsigned)N);
  }

  uint16_t mtu = ble_link_att_mtu();
  size_t max_payload = (mtu >= 23) ? (size_t)(mtu - 3) : 244;
  size_t budget = max_payload > C6_HEADER_LEN ? max_payload - C6_HEADER_LEN : 0;

  bool emitted_any = false;
  uint32_t i = 0;
  while (i < N) {
    c6_frame_t   frames[C6_FRAMES_PER_CHUNK];
    uint8_t      frame_vad[C6_FRAMES_PER_CHUNK];
    uint32_t     frame_rel_ts[C6_FRAMES_PER_CHUNK];
    uint8_t      count = 0;
    uint32_t     first_rel_ts = 0;
    size_t       used = 0;

    while (i < N) {
      uint32_t idx = start_idx + i;
      ring_frame_t f;
      if (!ring_buffer_get(idx, &f)) {
        /* overwritten mid-replay: suppress (chunk_seq still advances via packets) */
        i++;
        continue;
      }
      if (count == 0) first_rel_ts = f.rel_ts_ms;
      if (f.vad_state == C6_GAP_MARKER || f.len == 0) {
        i++;            /* suppress silence in body, like the live path */
        continue;
      }
      if (used + 1u + (size_t)f.len > budget) {
        if (count == 0) {
          /* single frame bigger than budget: drop it (matches live guard) */
          ESP_LOGW(TAG, "replay frame %u len %u > budget %u; dropping",
                   (unsigned)i, (unsigned)f.len, (unsigned)budget);
          i++;
        }
        break;          /* flush this packet, start a new one */
      }
      frames[count] = (c6_frame_t){ .data = f.data, .len = f.len };
      frame_vad[count] = f.vad_state;
      frame_rel_ts[count] = f.rel_ts_ms;
      used += 1u + (size_t)f.len;
      count++;
      i++;
    }

    bool last = (i >= N);
    uint8_t flags = last ? C6_FLAG_LAST_OF_REQ : 0;

    if (count == 0) {
      /* all-silence (or overwritten) span: emit one empty packet so chunk_seq
         advances and the request boundary is marked. */
      size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                                 first_rel_ts, (uint8_t)C6_GAP_MARKER, flags, frames, 0);
      if (n > 0) ble_link_notify_audio(pkt, n);
      (*chunk_seq)++;
      emitted_any = true;
    } else {
      size_t k = 0;
      while (k < count) {
        size_t start = k, seg_used = 0;
        while (k < count && seg_used + 1u + (size_t)frames[k].len <= budget) {
          seg_used += 1u + (size_t)frames[k].len;
          k++;
        }
        size_t n_in_pkt = k - start;
        if (n_in_pkt == 0) { k++; continue; }   /* unreachable given the outer check */
        bool pkt_last = last && (k >= count);
        uint8_t fl = pkt_last ? C6_FLAG_LAST_OF_REQ : 0;
        size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                                   frame_rel_ts[start], frame_vad[start], fl,
                                   &frames[start], n_in_pkt);
        if (n == 0) {
          ESP_LOGE(TAG, "replay packet overflow: n_in_pkt=%u cap=%u",
                   (unsigned)n_in_pkt, (unsigned)pkt_cap);
        } else {
          ble_link_notify_audio(pkt, n);
        }
        (*chunk_seq)++;
      }
      emitted_any = true;
    }
  }

  if (!emitted_any) {
    /* N == 0 (e.g. seconds=0 path / empty ring): emit one boundary packet. */
    size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                               0, (uint8_t)C6_GAP_MARKER, C6_FLAG_LAST_OF_REQ, frames, 0);
    if (n > 0) ble_link_notify_audio(pkt, n);
    (*chunk_seq)++;
  }
  ESP_LOGI(TAG, "replay done: seconds=%u frames=%u chunk_seq->%u",
           (unsigned)seconds, (unsigned)N, (unsigned)*chunk_seq);
}
```

**(c) Poll the replay queue at the top of the drain loop + create the queue in `ble_drain_start`.**

In `drain_task`, the loop currently starts with `for (;;) { uint32_t write_idx = ...`. Insert the replay poll as the FIRST thing inside `for (;;)`:

```c
  for (;;) {
    // Service any pending request_buffer replays before live draining. Replays
    // run to completion here (single owner of chunk_seq + notify), so live audio
    // is briefly delayed for the replay's duration (well under a second).
    replay_request_t rr;
    while (xQueueReceive(s_replay_queue, &rr, 0) == pdPASS) {
      drain_replay(rr.seconds, &chunk_seq, pkt, sizeof pkt);
    }

    uint32_t write_idx = ring_buffer_write_index();
    ...
```
(`chunk_seq` and `pkt` are already in scope in `drain_task` — `chunk_seq` is the local counter, `pkt` is the `static uint8_t pkt[PACKET_BUF_CAP]`. Reusing `pkt` is safe: the replay and the live branch never run concurrently — they're sequential within the same task.)

In `ble_drain_start`, create the queue before starting the task. Replace the function body:
```c
esp_err_t ble_drain_start(void) {
  s_replay_queue = xQueueCreate(DRAIN_REPLAY_QUEUE_DEPTH, sizeof(replay_request_t));
  if (s_replay_queue == NULL) {
    ESP_LOGE(TAG, "replay queue create failed");
    return ESP_FAIL;
  }
  BaseType_t ok = xTaskCreatePinnedToCore(
      drain_task, "drain", 16384, NULL, 5, NULL, 0);
  ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_FAIL, TAG, "xTaskCreatePinnedToCore failed");
  return ESP_OK;
}
```

- [ ] **Step 4: Build**

```bash
. ~/esp/esp-idf-v5.1.6/export.sh
cd /Users/kevin/Projects/Sense/firmware/sense_sensor
idf.py build
```
Expected: clean build, no warnings about the new code. If `freertos/queue.h` not found in `ble_drain.c`/`executor.c`, add `freertos` to `REQUIRES` in `main/CMakeLists.txt` (the IDF exposes it by default, so this should not be needed — but if it is, it's a one-line fix).

- [ ] **Step 5: Cross-check the window math agrees with the host test**

The `drain_replay` window math (`N = min(seconds*50, RING_FRAMES, write_idx)`, `start_idx = write_idx - N`) MUST equal `replay_window()` in `executor_core.c` (host-tested in Task 1). Diff the two by hand:
- `replay_window`: `want = seconds * (1000/FRAME_MS); N=min(want,RING_FRAMES,write_idx); *start=write_idx-N;`
- `drain_replay`: `want = seconds * (1000/FRAME_MS); N=min(want,RING_FRAMES,write_idx); start_idx=write_idx-N;`
They are identical. (If they ever diverge, the host test won't catch it — keep them in lockstep by copy-paste; do NOT "refactor" one to call the other, since `drain_replay` is ESP-only and `replay_window` is the host-tested reference.)

- [ ] **Step 6: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add firmware/sense_sensor/main/ble_drain.h firmware/sense_sensor/main/ble_drain.c \
        firmware/sense_sensor/main/config.h
git commit -m "$(cat <<'EOF'
feat(firmware): request_buffer drain replay (C6_MEMORY_CHUNK + LAST_OF_REQ)

Drain task owns a replay queue; the executor sends request_buffer
requests there. drain_replay emits the last N seconds of ring frames as
C6_MEMORY_CHUNK packets continuing the live chunk_seq, flags the final
packet C6_FLAG_LAST_OF_REQ. Live drain is briefly delayed for the replay
(single owner of chunk_seq + notify). Window math mirrors the host-tested
replay_window(). Completes the P4a request_buffer path end-to-end.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Bring-up runbook smoke steps + on-device verification

**Files:**
- Modify: `docs/bring-up/2026-07-27-real-device-bringup.md` (add a P4a executors smoke section)

**Interfaces:** none (documentation).

- [ ] **Step 1: Add a P4a smoke section to the runbook**

Edit `docs/bring-up/2026-07-27-real-device-bringup.md`. After the "Tier 3 — Speaker recognition" section (before "## Do not undo"), insert a new tier:

```markdown
---

## Tier 4 — Command executors (P4a: request_buffer + start/stop_audio)  [operator]

Validates the device-side executor framework + the three audio executors.
Requires Tiers 0–2 (a live gateway + relay path). The executors are exercised by
issuing signed commands from the server (the planner's `issue_command` path) or
the HTTP control API; the device acks and acts.

Start the gateway (Tier 0 command). Reconnect the relay. Then issue commands and
**watch the device monitor + gateway log**:

- **`stop_audio`** (e.g. ask the agent "stop listening for a bit", or POST an
  `issue_command` with `type=stop_audio`):
  - Device monitor: `exec: stop_audio`, then the audio per-second log shows
    `voiced=0 (no new speech) opus_bytes/s=0` and `cal e_pri=0 e_ref=0 ratio=0`.
  - Gateway: only empty live packets (`C6_GAP_MARKER`) arrive; no transcripts
    while paused. The ring keeps advancing (contiguous).
- **`start_audio`**:
  - Device monitor: `exec: start_audio`; speech at the device resumes
    transcription. `voiced>0` returns.
- **`request_buffer seconds=5`** (e.g. "play back the last 5 seconds"):
  - Device monitor: `exec` enqueues, then `drain: replay done: seconds=5 frames=250
    chunk_seq->N`. `C6_MEMORY_CHUNK` packets are notified, the last one with
    `C6_FLAG_LAST_OF_REQ`.
  - Gateway: a retrospective transcript segment appears for the last ~5 s
    (the reassembler orders the MEMORY_CHUNK packets by chunk_seq after the
    live head).
- **`request_buffer seconds=60` on a freshly-booted device** (<60 s of audio
  captured): device logs `replay capped: requested 60 s, have <3000> frames` and
  still emits a final `LAST_OF_REQ` packet.
- **`capture_photo` / `record_video`** (only if you manually issue one — they are
  in the server allowlist but not implemented in P4b yet):
  - Device monitor: `exec: capture_photo not implemented (P4b)`. The command acks
    (valid, authentic) but does nothing. Expected in P4a; do not file a bug.

**Exit criteria:** stop/start gate visibly silences/resumes transcription;
`request_buffer` produces a retrospective transcript segment with a clean
`LAST_OF_REQ` boundary; the capped-replay edge case logs correctly. Then the
P4a executors are verified on real hardware.
```

- [ ] **Step 2: Commit**

```bash
cd /Users/kevin/Projects/Sense
git add docs/bring-up/2026-07-27-real-device-bringup.md
git commit -m "$(cat <<'EOF'
docs(bring-up): Tier 4 P4a executor smoke (start/stop_audio, request_buffer)

Operator on-device verification steps for the P4a executors: stop/start
gate silences/resumes transcription; request_buffer emits a retrospective
transcript with a clean LAST_OF_REQ; capped replay logs. photo/video no-op
ack noted as expected P4a behavior.

Co-Authored-By: Claude <noreply@anthropic.com>
EOF
)"
```

- [ ] **Step 3: Sanity-run the server test suite (no server changes expected)**

```bash
cd /Users/kevin/Projects/Sense/server && source .venv/bin/activate && python -m pytest -q
```
Expected: green (P4a touches no server code). This is a regression guard only.

---

## Self-Review (run after writing the plan — issues found and fixed inline)

**1. Spec coverage:**
- §2 module boundaries → Tasks 1–3 create exactly those files (with `executor_core.{h,c}` replacing the spec's `executor_port.h` — noted in File Structure).
- §3 data flow → Task 2 wires `commands_handle` → `executor_submit` → executor task; Task 3 completes `request_buffer` → drain.
- §4 request_buffer (executor side + drain side) → Task 3 Step 3 implements `drain_replay` exactly per §4.2.1–§4.2.9.
- §5 start/stop_audio + audio_gate → Task 2 Steps 2–3 (audio_gate) + Step 7 (audio_task paused-branch with `goto frame_tail`).
- §6 edge cases → §6.1 (no-ack on full executor queue: Task 2 Step 6), §6.2 (BAD_PARAMS acks-no-work: Task 1 validation + Task 2 Step 5 handler drop), §6.3/§6.4 (capped replay: Task 3 Step 3 `drain_replay`), §6.5 (overwritten frame: Task 3 Step 3 `ring_buffer_get` false → suppress), §6.6 (no subscriber: notify returns <0, replay continues), §6.7 (no interaction), §6.8 (dedup: unchanged in commands.c), §6.9 (photo/video no-op ack: Task 2 Step 5 handler logs), §6.10 (drain replay queue full: Task 2 Step 5 handler logs).
- §7 config constants → Task 2 Step 1 + Task 3 Step 1.
- §8 concurrency → reflected in SPSC queue usage, single-owner chunk_seq, volatile bool gate.
- §9 testing → Task 1 host tests (all §9.1 cases); Task 4 §9.2 on-device smoke; Task 4 Step 3 §9.3 server regression.

**2. Placeholder scan:** none. All steps have real code or real commands.

**3. Type consistency:** `cmd_request_t` fields (`type`, `status`, `seconds`, `duration_s`) match across `executor_core.h` (Task 1), `executor.c` (Task 2), and `drain_replay` uses `replay_request_t.seconds` (Task 3). `executor_parse_and_validate` / `replay_window` signatures match Task 1 definitions and Task 2/3 usage. `ble_drain_replay_queue()` / `replay_request_t` defined in Task 3 Step 2, consumed in Task 2 Step 5 (forward reference — noted: build deferred to Task 3).

**4. Ambiguity:** the `goto frame_tail` label placement is pinned (before `frames++; rel_ts_ms += FRAME_MS;`) and the per-second/stack blocks running for paused frames is called out. The drain_replay window-math agreement with `replay_window()` is cross-checked in Task 3 Step 5.

No gaps found. Plan is complete.