# Dual IENMP441 Voice-Isolating Microphone Array — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the single PDM mic with a two-mic I2S array (shared SCK/WS/SD bus) and add on-device NLMS differential noise cancellation + dual-channel ratio VAD, so the device streams clean mono speech over the unchanged §C.6/Opus uplink with minimal junk and interruptions in noisy environments.

**Architecture:** `audio_capture` becomes an I2S-standard stereo reader (3 GPIOs, both mics on one bus, L/R channel-select picks primary vs reference). A new pure-C `mic_dsp` module does Widrow NLMS adaptive noise cancellation (filter adapts only on noise-only frames, frozen during speech). `vad` gains a dual-channel ratio test that rejects loud correlated ambient. The cleaned mono feeds the existing Opus → ring → §C.6 → BLE path unchanged. `ble_drain`, `c6_packet`, `ble_link` are untouched.

**Tech Stack:** ESP-IDF v5.1.6 (NimBLE 1.6), ESP32-S3 (XIAO ESP32S3 Sense), `driver/i2s_std.h`, libopus 1.5.2, host C contract tests (`cc -std=c11`).

## Global Constraints

- ESP-IDF pinned to **v5.1.6** (NimBLE 1.6); the I2S component is `driver` (NOT `esp_driver_i2s` — that's IDF 6.0+). Install via `firmware/sense_sensor/scripts/install_idf_5.1.6.sh`.
- `CONFIG_SPIRAM_USE_CAPS_ALLOC=y` (NOT `SPIRAM_USE_MALLOC`) is preserved — NimBLE queues/mutexes must stay in internal SRAM (the `xQueueSemaphoreTake uxItemSize==0` assert workaround). Do not change `sdkconfig.defaults` PSRAM settings.
- `config.h` stays **pure C / host-compilable** (only `<stdint.h>` and integer macros). No ESP headers. Host tests `#include "config.h"`.
- Host-testable modules (`mic_dsp.c`, `vad.c`) must have **no ESP dependencies** — only `config.h` + standard C. They compile with `cc -std=c11 -Wall -Wextra -I../main`.
- The uplink contract is **unchanged**: mono 16 kHz Opus 24 kbps over §C.6. `CHANNELS` stays `1`, `FRAME_SAMPLES` stays `320`, `FRAME_BYTES` stays `640`. Do not touch `ble_drain.c`, `c6_packet.c`, `ble_link.c`, `opus_stream.c`, `ring_buffer.c`.
- microSD pins (GPIO 7/8/9/21) stay intact. The new mic uses **GPIO 4/5/6**.
- New tunables are initial defaults marked "TUNE ON HARDWARE"; they are finalized in the Task 5 smoke step. Do not pretend they are final.

---

## File Structure

| File | Responsibility | Task |
|---|---|---|
| `main/config.h` | Add DSP/VAD-ratio/I2S-pin defines; remove PDM defines | 1, 2, 3 |
| `main/mic_dsp.h` (new) | `mic_dsp_t` + `mic_dsp_init` / `mic_dsp_process` (pure C, host-testable) | 1 |
| `main/mic_dsp.c` (new) | NLMS adaptive differential cancellation, int32 Q15 fixed-point | 1 |
| `test/test_mic_dsp.c` (new) | Host contract tests for cancellation / freeze / fallback / stability | 1 |
| `test/Makefile` | Add `dsp` target | 1 |
| `main/CMakeLists.txt` | Add `mic_dsp.c` to SRCS | 1 |
| `main/vad.h` | Add `ratio_threshold` field; declare `vad_process_dual` | 2 |
| `main/vad.c` | Set `ratio_threshold` in `vad_init`; implement `vad_process_dual` | 2 |
| `test/test_vad.c` | Add dual-channel tests | 2 |
| `main/audio_capture.h` | Replace `read_frame` with `read_stereo` | 3 |
| `main/audio_capture.c` | Swap `i2s_pdm` → `i2s_std` stereo RX on GPIO 4/5/6 | 3 |
| `main/sense_sensor.c` | Rewire `audio_task`: read_stereo → vad_process_dual → mic_dsp → opus(mono); remove dead `vad_process` use | 3, 4 |
| `firmware/sense_sensor/README.md` (or `config.h` comments) | Pin map, L/R wiring, tuning notes | 5 |

Each module has one responsibility and is host-testable in isolation except `audio_capture` (ESP I2S driver) and `sense_sensor.c` (task wiring), which are verified by `idf.py build` + on-device smoke.

---

### Task 1: `mic_dsp` — NLMS adaptive differential cancellation (host-tested)

**Files:**
- Create: `firmware/sense_sensor/main/mic_dsp.h`
- Create: `firmware/sense_sensor/main/mic_dsp.c`
- Create: `firmware/sense_sensor/test/test_mic_dsp.c`
- Modify: `firmware/sense_sensor/main/config.h` (add DSP defines)
- Modify: `firmware/sense_sensor/test/Makefile` (add `dsp` target)
- Modify: `firmware/sense_sensor/main/CMakeLists.txt` (add `mic_dsp.c` to SRCS)

**Interfaces:**
- Consumes: `config.h` macros `DSP_NLMS_TAPS`, `DSP_NLMS_STEP_Q15`, `DSP_NLMS_LEAK_Q15`, `DSP_NLMS_EPS` (added in this task).
- Produces: `mic_dsp_t`, `mic_dsp_init(mic_dsp_t *d)`, `mic_dsp_process(mic_dsp_t *d, const int16_t *primary, const int16_t *reference, int16_t *out, size_t n, bool adapt_now)`. Task 4 wires this into `audio_task`.

- [ ] **Step 1: Add the DSP tunables to `config.h`**

Insert after the VAD block (after line 37, the `VAD_ENERGY_THRESHOLD` define), before the §C.6 block:

```c
/* ---- Dual-mic DSP: NLMS adaptive differential noise cancellation ----
 * Pure fixed-point (int32 Q15). The reference mic (back, ambient) drives an
 * adaptive FIR that models the noise path to the primary (front, voice) mic;
 * the filtered reference is subtracted from the primary. The filter adapts
 * ONLY on noise-only frames (VAD says C6_GAP_MARKER) and is frozen during
 * speech so it cancels noise, not voice. All values TUNE ON HARDWARE. */
#define DSP_NLMS_TAPS       32        /* filter order; 2 ms at 16 kHz */
#define DSP_NLMS_STEP_Q15   6553      /* normalized step ~0.2 in Q15 */
#define DSP_NLMS_LEAK_Q15   1         /* leakage (~3e-5) to bound the filter */
#define DSP_NLMS_EPS        512       /* ref-power regularization (same scale as ||x||^2) */
```

- [ ] **Step 2: Write `mic_dsp.h`**

```c
/*
 * Dual-mic DSP — adaptive differential noise cancellation (pure C, host-testable).
 *
 * Widrow NLMS noise canceller in int32 Q15 fixed-point. The reference mic faces
 * ambient; the primary mic faces the voice. y[n] = primary[n] - sum_k w[k]*ref[n-k].
 * The filter w adapts only when adapt_now is true (noise-only frames, gated by the
 * VAD in the caller); it is frozen during speech so voice is never cancelled.
 *
 * No ESP dependencies: only config.h + standard C, so it builds under the host
 * contract test the same way as vad.c / c6_packet.c.
 */
#pragma once

#include "config.h"

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int32_t w[DSP_NLMS_TAPS];      /* Q15 filter coeffs (reference -> primary noise path) */
  int16_t delay[DSP_NLMS_TAPS];  /* reference sample delay ring */
  int      didx;                 /* delay ring write index */
} mic_dsp_t;

/* Zero the filter and delay line. */
void mic_dsp_init(mic_dsp_t *d);

/* Process one frame of `n` samples. out gets the cleaned mono (Q0 int16).
 * When adapt_now, the filter is NLMS-updated from the residual (call this only
 * on noise-only frames). When false, w is held and the last-learned noise path
 * is applied. `primary`, `reference`, `out` each hold `n` int16 samples. */
void mic_dsp_process(mic_dsp_t *d, const int16_t *primary, const int16_t *reference,
                     int16_t *out, size_t n, bool adapt_now);

#ifdef __cplusplus
}
#endif
```

- [ ] **Step 3: Write `mic_dsp.c`**

```c
#include "mic_dsp.h"

static inline int16_t sat_int16(int32_t v) {
  if (v > 32767)  return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void mic_dsp_init(mic_dsp_t *d) {
  for (int k = 0; k < DSP_NLMS_TAPS; k++) {
    d->w[k] = 0;
    d->delay[k] = 0;
  }
  d->didx = 0;
}

void mic_dsp_process(mic_dsp_t *d, const int16_t *primary, const int16_t *reference,
                     int16_t *out, size_t n, bool adapt_now) {
  const int L = DSP_NLMS_TAPS;
  for (size_t i = 0; i < n; i++) {
    /* Push newest reference sample into the delay ring at didx. */
    int16_t x = reference[i];
    d->delay[d->didx] = x;

    /* Filtered reference: y_q15 = sum_k w[k] * delay[(didx - k + L) % L].
     * w is Q15, delay is Q0 (int16) -> product is Q15; accumulate in int64. */
    int64_t acc = 0;
    int64_t pow = 0;  /* ||delay||^2, Q0 (sum of int16^2) */
    for (int k = 0; k < L; k++) {
      int idx = (d->didx - k + L) % L;   /* lag-k sample */
      int32_t dk = d->delay[idx];
      acc += (int64_t)d->w[k] * (int64_t)dk;
      pow += (int64_t)dk * (int64_t)dk;
    }
    int32_t y_q15 = (int32_t)(acc >> 15);
    int32_t y = (int32_t)primary[i] - y_q15;   /* cleaned output (Q0) */
    out[i] = sat_int16(y);

    if (adapt_now) {
      /* Widrow: the adaptation error is the output itself (minimize output
       * power on noise-only frames -> drive filter toward the noise path). */
      int32_t e = y;
      int64_t denom = pow + (int64_t)DSP_NLMS_EPS;
      if (denom > 0) {
        for (int k = 0; k < L; k++) {
          int idx = (d->didx - k + L) % L;
          /* NLMS in Q15: w[k] += mu_q15 * e * x[k] / (||x||^2 + eps) */
          int64_t upd = ((int64_t)DSP_NLMS_STEP_Q15 * (int64_t)e * (int64_t)d->delay[idx]) / denom;
          d->w[k] = (int32_t)(d->w[k] + upd);
          /* leakage: gently pull w toward 0 to bound drift */
          d->w[k] -= (d->w[k] * (int32_t)DSP_NLMS_LEAK_Q15) >> 15;
        }
      }
    }

    d->didx = (d->didx + 1) % L;
  }
}
```

- [ ] **Step 4: Write `test/test_mic_dsp.c`**

```c
/*
 * Host-side test for the dual-mic NLMS canceller (no ESP toolchain).
 *   cc -std=c11 -I../main ../main/mic_dsp.c test_mic_dsp.c -o /tmp/dsptest && /tmp/dsptest
 */
#include "config.h"
#include "mic_dsp.h"

#include <stdio.h>
#include <string.h>

#define N FRAME_SAMPLES

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* Deterministic LCG noise in [-amp, +amp] (no math.h / -lm needed). */
static void gen_noise(int16_t *buf, int amp, unsigned *seed) {
  for (int i = 0; i < N; i++) {
    *seed = *seed * 1664525u + 1013904223u;
    int v = (int)(*seed >> 16) % (2 * amp + 1) - amp;
    buf[i] = (int16_t)v;
  }
}

/* 800 Hz square wave at 16 kHz (period 20 samples): voice stand-in. */
static void gen_voice(int16_t *buf, int amp) {
  for (int i = 0; i < N; i++)
    buf[i] = ((i / 10) % 2) ? (int16_t)amp : (int16_t)-amp;
}

static uint64_t energy(const int16_t *b) {
  uint64_t s = 0;
  for (int i = 0; i < N; i++) { int32_t v = b[i]; s += (uint64_t)(v * v); }
  return s / N;
}

int main(void) {
  printf("mic_dsp host test\n");

  /* ---- Test 1: cancellation of correlated noise ---- */
  {
    mic_dsp_t dsp;
    mic_dsp_init(&dsp);
    int16_t noise[N], voice[N], pri[N], ref[N], out[N];
    gen_voice(voice, 500);
    unsigned seed = 12345u;

    /* Warm-up: noise-only frames, reference == primary == noise, adapt on.
     * The filter should learn w[0] ~ 1 (identity noise path). */
    for (int f = 0; f < 20; f++) {
      gen_noise(noise, 3000, &seed);
      for (int i = 0; i < N; i++) { ref[i] = noise[i]; pri[i] = noise[i]; }
      mic_dsp_process(&dsp, pri, ref, out, N, true);
    }

    /* Test frame: primary = voice + noise, reference = noise, adapt FROZEN. */
    gen_noise(noise, 3000, &seed);
    for (int i = 0; i < N; i++) { ref[i] = noise[i]; pri[i] = (int16_t)(voice[i] + noise[i]); }
    mic_dsp_process(&dsp, pri, ref, out, N, false);

    uint64_t e_pri = energy(pri), e_out = energy(out), e_voice = energy(voice);
    printf("  pri_energy=%llu out_energy=%llu voice_energy=%llu\n",
           (unsigned long long)e_pri, (unsigned long long)e_out, (unsigned long long)e_voice);
    /* Noise removed: out should be far below primary. */
    check("cancellation: out << primary (>=12 dB power)", e_out * 16 < e_pri);
    /* Voice preserved: out should be on the order of the voice energy. */
    check("voice preserved: out ~= voice", e_out < 4 * e_voice);
  }

  /* ---- Test 2: freeze during speech (coeffs unchanged) ---- */
  {
    mic_dsp_t dsp;
    mic_dsp_init(&dsp);
    int16_t noise[N], pri[N], ref[N], out[N];
    unsigned seed = 999u;
    for (int f = 0; f < 10; f++) {
      gen_noise(noise, 3000, &seed);
      for (int i = 0; i < N; i++) { ref[i] = noise[i]; pri[i] = noise[i]; }
      mic_dsp_process(&dsp, pri, ref, out, N, true);
    }
    int32_t snap[DSP_NLMS_TAPS];
    memcpy(snap, dsp.w, sizeof snap);
    /* Feed a frame with adapt frozen; w must not move. */
    gen_noise(noise, 3000, &seed);
    for (int i = 0; i < N; i++) { ref[i] = noise[i]; pri[i] = noise[i]; }
    mic_dsp_process(&dsp, pri, ref, out, N, false);
    check("filter frozen during speech", memcmp(snap, dsp.w, sizeof snap) == 0);
  }

  /* ---- Test 3: silent reference -> output ~= primary (graceful fallback) ---- */
  {
    mic_dsp_t dsp;
    mic_dsp_init(&dsp);
    int16_t pri[N], ref[N], out[N];
    unsigned seed = 7u;
    gen_noise(pri, 2000, &seed);
    for (int i = 0; i < N; i++) ref[i] = 0;
    mic_dsp_process(&dsp, pri, ref, out, N, true);  /* adapt on, but x==0 so no update */
    int bad = 0;
    for (int i = 0; i < N; i++) if (out[i] != pri[i]) bad++;
    check("reference silent -> out == primary", bad == 0);
  }

  /* ---- Test 4: long-run stability (filter stays bounded) ---- */
  {
    mic_dsp_t dsp;
    mic_dsp_init(&dsp);
    int16_t noise[N], voice[N], pri[N], ref[N], out[N];
    gen_voice(voice, 500);
    unsigned seed = 4242u;
    for (int f = 0; f < 3000; f++) {           /* 60 s */
      gen_noise(noise, 3000, &seed);
      for (int i = 0; i < N; i++) ref[i] = noise[i];
      if (f % 2 == 0) {                         /* noise-only frame: adapt */
        for (int i = 0; i < N; i++) pri[i] = noise[i];
        mic_dsp_process(&dsp, pri, ref, out, N, true);
      } else {                                  /* speech+noise frame: frozen */
        for (int i = 0; i < N; i++) pri[i] = (int16_t)(voice[i] + noise[i]);
        mic_dsp_process(&dsp, pri, ref, out, N, false);
      }
    }
    int32_t maxw = 0;
    for (int k = 0; k < DSP_NLMS_TAPS; k++) {
      int32_t a = dsp.w[k] < 0 ? -dsp.w[k] : dsp.w[k];
      if (a > maxw) maxw = a;
    }
    printf("  max|w|=%ld\n", (long)maxw);
    check("filter bounded after 60 s run", maxw < 40000);  /* < ~1.22 in Q15 */
  }

  printf(failures ? "\nFAILED (%d)\n" : "\nOK\n", failures);
  return failures ? 1 : 0;
}
```

- [ ] **Step 5: Add the `dsp` target to `test/Makefile`**

Change the `test:` line and add the target + clean entry:

```make
test: c6 vad dsp provisioning
```

```make
dsp: /tmp/dsptest
	/tmp/dsptest
```

```make
/tmp/dsptest: test_mic_dsp.c ../main/mic_dsp.c ../main/mic_dsp.h ../main/config.h
	$(CC) $(CFLAGS) ../main/mic_dsp.c test_mic_dsp.c -o $@
```

And append `/tmp/dsptest` to the `clean:` `rm -f` line.

- [ ] **Step 6: Add `mic_dsp.c` to the firmware build**

In `main/CMakeLists.txt`, add `"mic_dsp.c"` to the `SRCS` list (after `"vad.c"`).

- [ ] **Step 7: Run the host test and verify it passes**

Run: `cd firmware/sense_sensor/test && make dsp`
Expected: `OK` (all four checks PASS).

If a threshold is off by a little (e.g. `e_out * 16 < e_pri` nearly misses), tune the assertion constant in the test — the DSP behavior is what is being locked in, not the exact ratio. Re-run until green.

- [ ] **Step 8: Run the full host test suite to confirm nothing else broke**

Run: `cd firmware/sense_sensor/test && make`
Expected: c6, vad, dsp, provisioning all pass.

- [ ] **Step 9: Commit**

```bash
git add firmware/sense_sensor/main/config.h firmware/sense_sensor/main/mic_dsp.h \
        firmware/sense_sensor/main/mic_dsp.c firmware/sense_sensor/main/CMakeLists.txt \
        firmware/sense_sensor/test/test_mic_dsp.c firmware/sense_sensor/test/Makefile
git commit -m "feat(firmware): add NLMS dual-mic noise canceller (mic_dsp)

Pure-C host-tested Widrow NLMS adaptive differential canceller (int32 Q15).
Filter adapts only on noise-only frames (frozen during speech) so it cancels
ambient noise from the reference mic without touching voice on the primary.
Tunables TUNE ON HARDWARE. Wired into audio_task in a later task.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 2: Dual-channel ratio VAD (host-tested)

**Files:**
- Modify: `firmware/sense_sensor/main/config.h` (add `VAD_RATIO_THRESHOLD`)
- Modify: `firmware/sense_sensor/main/vad.h` (add `ratio_threshold` field + `vad_process_dual`)
- Modify: `firmware/sense_sensor/main/vad.c` (set ratio in `vad_init`; implement `vad_process_dual`)
- Modify: `firmware/sense_sensor/test/test_vad.c` (add dual-channel tests)

**Interfaces:**
- Consumes: `config.h` macro `VAD_RATIO_THRESHOLD` (added here). `vad_init` signature is **unchanged** (`vad_init(vad_t*, uint32_t energy_threshold, int hangover_frames)`) so `sense_sensor.c` keeps compiling.
- Produces: `vad_process_dual(vad_t *vad, const int16_t *primary, const int16_t *reference, size_t n)` returning a `c6_vad_state`. The existing `vad_process` stays (removed in Task 4 once `audio_task` switches over). `vad_t` gains a `ratio_threshold` field populated from `VAD_RATIO_THRESHOLD` inside `vad_init`.

- [ ] **Step 1: Add `VAD_RATIO_THRESHOLD` to `config.h`**

In the VAD block, after `VAD_ENERGY_THRESHOLD`:

```c
/* Dual-channel ratio gate: primary energy must exceed reference energy by this
 * factor to count as speech. Loud ambient that lands similarly on both mics
 * (ratio ~1) is rejected even when it passes the energy threshold. TUNE ON HARDWARE. */
#define VAD_RATIO_THRESHOLD 4u
```

- [ ] **Step 2: Extend `vad.h`**

Add the `ratio_threshold` field to `vad_t` and declare `vad_process_dual`. Do **not** remove `vad_process` yet.

```c
typedef struct {
  uint32_t energy_threshold;  /* per-sample mean-square gate on primary */
  uint32_t ratio_threshold;   /* primary/ref energy ratio gate (e_pri > ratio*(e_ref+1)) */
  int      hangover_frames;   /* configured hangover length */
  int      hangover_left;     /* remaining hangover countdown */
} vad_t;

/* Initialise with an energy threshold and hangover length (in frames).
 * ratio_threshold is populated from VAD_RATIO_THRESHOLD. Signature unchanged
 * so existing callers keep compiling. */
void vad_init(vad_t *vad, uint32_t energy_threshold, int hangover_frames);

/* Classify one frame of `n` int16 samples; returns a c6_vad_state value. */
uint8_t vad_process(vad_t *vad, const int16_t *pcm, size_t n);

/* Dual-channel classifier: speech iff primary energy > energy_threshold AND
 * primary energy > ratio_threshold * (reference energy + 1). Same hangover
 * state machine as vad_process. Returns a c6_vad_state. */
uint8_t vad_process_dual(vad_t *vad, const int16_t *primary, const int16_t *reference, size_t n);
```

- [ ] **Step 3: Implement `vad_process_dual` in `vad.c`**

In `vad_init`, add `vad->ratio_threshold = VAD_RATIO_THRESHOLD;`. Keep `vad_process` as-is. Add:

```c
uint8_t vad_process_dual(vad_t *vad, const int16_t *primary, const int16_t *reference, size_t n) {
  uint64_t sp = 0, sr = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t p = primary[i];
    int32_t r = reference[i];
    sp += (uint64_t)(p * p);
    sr += (uint64_t)(r * r);
  }
  uint32_t e_pri = (n > 0) ? (uint32_t)(sp / n) : 0;
  uint32_t e_ref = (n > 0) ? (uint32_t)(sr / n) : 0;
  /* Avoid float + overflow: compare in uint64. ratio = e_pri/(e_ref+1) > R
   *  <=>  e_pri > R * (e_ref + 1). */
  int speech = (e_pri > vad->energy_threshold) &&
               ((uint64_t)e_pri > (uint64_t)vad->ratio_threshold * (uint64_t)(e_ref + 1));

  if (speech) {
    vad->hangover_left = vad->hangover_frames;
    return C6_SPEECH;
  }
  if (vad->hangover_left > 0) {
    vad->hangover_left--;
    return C6_HANGOVER;
  }
  return C6_GAP_MARKER;
}
```

- [ ] **Step 4: Add dual-channel tests to `test/test_vad.c`**

Append a new `main`-section block (or extend `main`). Keep the existing single-channel tests (they still exercise `vad_process`, which still exists). Add at the end of `main`, before the final `printf`/`return`:

```c
  /* ---- Dual-channel ratio VAD ---- */
  printf("dual-channel VAD host test\n");
  {
    const uint32_t threshold = 1000000;  /* mean-square gate (amp 1000 -> 1e6) */
    const int hangover = 3;
    int16_t loud_p[N], loud_r[N], quiet[N], voice_p[N], amb_r[N];
    fill(loud_p, 2000);  fill(loud_r, 2000);   /* loud correlated ambient on BOTH */
    fill(quiet, 100);
    fill(voice_p, 2000);                       /* voice on primary, above threshold */
    fill(amb_r, 500);                          /* mild ambient on reference */

    vad_t vd;
    vad_init(&vd, threshold, hangover);

    /* Loud correlated ambient: passes energy gate, ratio ~1 -> rejected. */
    check("loud correlated ambient -> GAP (junk rejected)",
          vad_process_dual(&vd, loud_p, loud_r, N) == C6_GAP_MARKER);

    /* Voice above threshold, mild ambient on reference: ratio high -> SPEECH. */
    check("voice+ambient -> SPEECH",
          vad_process_dual(&vd, voice_p, amb_r, N) == C6_SPEECH);

    /* Reference dead (e_ref ~ 0): ratio -> inf -> pure energy VAD fallback. */
    check("reference dead, loud primary -> SPEECH",
          vad_process_dual(&vd, loud_p, quiet, N) == C6_SPEECH);

    /* Hangover after speech ends. */
    check("hangover 1", vad_process_dual(&vd, quiet, quiet, N) == C6_HANGOVER);
    check("hangover 2", vad_process_dual(&vd, quiet, quiet, N) == C6_HANGOVER);
    check("hangover 3", vad_process_dual(&vd, quiet, quiet, N) == C6_HANGOVER);
    check("then GAP", vad_process_dual(&vd, quiet, quiet, N) == C6_GAP_MARKER);

    /* Cold start in correlated ambient is a gap, not spurious speech. */
    vad_t vd2;
    vad_init(&vd2, threshold, hangover);
    check("cold start ambient -> GAP",
          vad_process_dual(&vd2, loud_p, loud_r, N) == C6_GAP_MARKER);
  }
```

- [ ] **Step 5: Run the host test and verify it passes**

Run: `cd firmware/sense_sensor/test && make vad`
Expected: `OK` (all checks PASS, both the original single-channel set and the new dual-channel set).

- [ ] **Step 6: Run the full host suite**

Run: `cd firmware/sense_sensor/test && make`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add firmware/sense_sensor/main/config.h firmware/sense_sensor/main/vad.h \
        firmware/sense_sensor/main/vad.c firmware/sense_sensor/test/test_vad.c
git commit -m "feat(firmware): dual-channel ratio VAD (vad_process_dual)

Speech iff primary energy > energy_threshold AND primary > ratio*reference.
Loud correlated ambient (ratio ~1) is rejected as gap instead of speech,
which is the junk-rejection the single-channel energy VAD missed. Falls back
to energy-only VAD when the reference mic is silent. vad_init signature is
unchanged; vad_process stays until audio_task switches over in a later task.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 3: I2S standard stereo capture on GPIO 4/5/6

**Files:**
- Modify: `firmware/sense_sensor/main/config.h` (add I2S pin defines + `PRIMARY_CHANNEL`; remove `PDM_*`)
- Modify: `firmware/sense_sensor/main/audio_capture.h` (replace `read_frame` with `read_stereo`)
- Modify: `firmware/sense_sensor/main/audio_capture.c` (swap `i2s_pdm` → `i2s_std` stereo)
- Modify: `firmware/sense_sensor/main/sense_sensor.c` (call `read_stereo`; keep `vad_process` on the primary for now — DSP/dual-VAD land in Task 4)

**Interfaces:**
- Consumes: `config.h` macros `I2S_BCK_GPIO`, `I2S_WS_GPIO`, `I2S_DATA_GPIO`, `PRIMARY_CHANNEL` (added here).
- Produces: `audio_capture_read_stereo(int16_t *primary, int16_t *reference)` returning `esp_err_t`; each buffer holds `FRAME_SAMPLES` int16 samples. `audio_capture_init` unchanged signature. The old `audio_capture_read_frame` is removed.

**Note:** This task changes on-device hardware behavior and cannot be fully verified without a flashed device. The deliverable is: `idf.py build` succeeds, and (with hardware) the on-device smoke in Step 7 shows per-channel energy. If no device is available, stop after a successful build + code review and note that smoke is pending.

- [ ] **Step 1: Replace the PDM pin block in `config.h`**

Delete the `PDM microphone pins` block (lines 73-75):

```c
/* ---- PDM microphone pins (XIAO ESP32S3 Sense onboard mic) ---- */
#define PDM_CLK_GPIO 42
#define PDM_DIN_GPIO 41
```

Replace with:

```c
/* ---- Dual IENMP441 I2S microphone array (shared bus) ----
 * Two I2S MEMS mics share SCK + WS + SD; each mic's L/R channel-select pin
 * ties one to the left slot (GND) and the other to the right slot (VDD).
 * PRIMARY_CHANNEL picks which deinterleaved channel is the voice/primary mic
 * (front, faces wearer) vs the noise reference (back, faces ambient). Flip by
 * changing this constant — no rewiring. Pins chosen to avoid the microSD SPI
 * bus (GPIO 7/8/9/21), strapping pins (0/3/45/46), and flash/PSRAM (26-32). */
#define I2S_BCK_GPIO    4   /* SCK  (D3) */
#define I2S_WS_GPIO     5   /* LRCLK (D4) */
#define I2S_DATA_GPIO   6   /* SD   (D5) */
#define PRIMARY_CHANNEL 0   /* 0 = left is voice/primary, 1 = right is voice/primary */
```

- [ ] **Step 2: Rewrite `audio_capture.h`**

```c
/*
 * Audio capture — I2S standard stereo RX from the dual IENMP441 array via DMA.
 *
 * Two MEMS mics on a shared I2S bus (SCK/WS/SD); L/R channel-select splits them
 * into left (primary/voice) and right (reference/ambient). Thin wrapper over the
 * IDF I2S driver: stereo RX channel with DMA buffers sized to one 20 ms frame
 * per channel, deinterleaved into primary + reference. The audio task (core 1)
 * calls read_stereo() in a loop.
 */
#pragma once

#include "config.h"
#include "esp_err.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Configure and enable the I2S std stereo RX channel. Call once at startup.
esp_err_t audio_capture_init(void);

// Block until one full 20 ms stereo frame is read and deinterleaved into
// `primary` (voice mic) and `reference` (ambient mic). Each must hold
// FRAME_SAMPLES int16 samples. Returns ESP_ERR_INVALID_SIZE on a short read.
esp_err_t audio_capture_read_stereo(int16_t *primary, int16_t *reference);

#ifdef __cplusplus
}
#endif
```

- [ ] **Step 3: Rewrite `audio_capture.c`**

```c
#include "audio_capture.h"

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio";

static i2s_chan_handle_t s_rx_chan;

esp_err_t audio_capture_init(void) {
  // One RX channel; DMA buffers sized to a frame so each read returns ~20 ms.
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = 6;
  chan_cfg.dma_frame_num = FRAME_SAMPLES;   // 320 frames per channel per DMA desc
  ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan), TAG, "new channel");

  i2s_std_clk_config_t clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE);
  i2s_std_gpio_config_t gpio_cfg = {
      .sck = I2S_BCK_GPIO,
      .ws  = I2S_WS_GPIO,
      .dout = -1,
      .din  = I2S_DATA_GPIO,
      .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
  };
  // Philips stereo, 16-bit, both slots. slot_mask keeps L+R.
  i2s_std_slot_config_t slot_cfg = I2S_STD_PHILIPS_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                                  I2S_SLOT_MODE_STEREO);
  i2s_std_rx_config_t rx_cfg = {
      .clk_cfg  = &clk_cfg,
      .slot_cfg = &slot_cfg,
      .gpio_cfg = &gpio_cfg,
  };
  ESP_RETURN_ON_ERROR(i2s_channel_init_std_rx_mode(s_rx_chan, &rx_cfg), TAG, "init std rx");
  ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_chan), TAG, "enable");
  ESP_LOGI(TAG, "I2S STD RX up: %d Hz stereo, sck=%d ws=%d din=%d primary=ch%d",
           SAMPLE_RATE, I2S_BCK_GPIO, I2S_WS_GPIO, I2S_DATA_GPIO, PRIMARY_CHANNEL);
  return ESP_OK;
}

esp_err_t audio_capture_read_stereo(int16_t *primary, int16_t *reference) {
  // One 20 ms stereo frame: 320 L + 320 R = 640 int16 = 1280 bytes, interleaved.
  static int16_t stereo[FRAME_SAMPLES * 2];
  size_t want = FRAME_SAMPLES * 2 * sizeof(int16_t);
  size_t bytes_read = 0;
  esp_err_t err = i2s_channel_read(s_rx_chan, stereo, want, &bytes_read, portMAX_DELAY);
  if (err != ESP_OK) return err;
  if (bytes_read != want) return ESP_ERR_INVALID_SIZE;

  // Deinterleave: channel 0 = left, channel 1 = right.
  for (size_t i = 0; i < FRAME_SAMPLES; i++) {
    int16_t l = stereo[2 * i];
    int16_t r = stereo[2 * i + 1];
    if (PRIMARY_CHANNEL == 0) {
      primary[i] = l;
      reference[i] = r;
    } else {
      primary[i] = r;
      reference[i] = l;
    }
  }
  return ESP_OK;
}
```

- [ ] **Step 4: Update `audio_task` in `sense_sensor.c` to read stereo (keep mono VAD/DSP path for now)**

Change the buffers and the read call; keep `vad_process` on `primary` and Opus-encode `primary`. DSP + dual-VAD land in Task 4.

Replace `static int16_t pcm[FRAME_SAMPLES];` (line 49) with:

```c
  static int16_t pri[FRAME_SAMPLES];
  static int16_t ref[FRAME_SAMPLES];
  static uint8_t opus_buf[MAX_OPUS_BYTES];
```

(Keep the existing `static uint8_t opus_buf[MAX_OPUS_BYTES];` line — only `pcm` is replaced; `ref` is added and unused until Task 4. Mark it `(void)ref;` once to avoid an unused warning, or just let Task 4 use it. To stay warning-clean now, reference it:)

```c
  (void)ref;  /* used by Task 4 (dual-VAD + DSP); read now to keep the buffer warm */
```

Replace the read call (line 62) `if (audio_capture_read_frame(pcm) != ESP_OK)` with:

```c
    if (audio_capture_read_stereo(pri, ref) != ESP_OK) {
      continue;  // DMA not ready yet; retry next tick
    }
```

Replace `vad_process(&vad, pcm, FRAME_SAMPLES)` (line 66) with `vad_process(&vad, pri, FRAME_SAMPLES)`, and the Opus encode `opus_stream_encode(pcm, ...)` (line 72) with `opus_stream_encode(pri, opus_buf, sizeof opus_buf)`.

Update the boot log in `app_main` (line 109): change `"%d Hz mono"` to `"%d Hz stereo (dual-mic)"`.

- [ ] **Step 5: Build the firmware**

Run (with the IDF 5.1.6 env sourced): `cd firmware/sense_sensor && idf.py build`
Expected: build succeeds with no errors. Fix any compile issues (e.g. `i2s_std.h` include path, slot macro names — in IDF 5.1.6 the macro is `I2S_STD_PHILIPS_DEFAULT_CONFIG`).

- [ ] **Step 6: Run the host test suite (sanity — no host files should have changed, but confirm)**

Run: `cd firmware/sense_sensor/test && make`
Expected: all pass. (Task 3 touches no host-testable code, but `config.h` changed — confirm host tests still compile against the new defines.)

- [ ] **Step 7: On-device smoke (requires hardware)**

Flash and monitor: `idf.py -p <PORT> flash monitor`. With the dual-mic wired (SCK→D3, WS→D4, SD→D5; mic #1 L/R→GND, mic #2 L/R→VDD; both VDD→3V3, GND→GND):
- Confirm boot log: `I2S STD RX up: 16000 Hz stereo, sck=4 ws=5 din=6 primary=ch0`.
- Confirm the per-second `audio:` line shows `voiced` counts tracking speech, not constant ambient (if voiced fires constantly on ambient, the energy threshold needs tuning — but the ratio gate arrives in Task 4, so some ambient false-triggers are expected here).
- Tap/speak near the front mic; confirm `voiced` rises. Cover the front mic and expose the back mic to noise; confirm `voiced` is lower (primary is quieter).

If no device is available, note "smoke pending hardware" in the commit message and proceed.

- [ ] **Step 8: Commit**

```bash
git add firmware/sense_sensor/main/config.h firmware/sense_sensor/main/audio_capture.h \
        firmware/sense_sensor/main/audio_capture.c firmware/sense_sensor/main/sense_sensor.c
git commit -m "feat(firmware): I2S standard stereo capture on GPIO 4/5/6

Swap the single PDM mic (GPIO 41/42) for a dual IENMP441 array on a shared
I2S bus (SCK=4/WS=5/SD=6). L/R channel-select splits the mics into primary
(voice) and reference (ambient); PRIMARY_CHANNEL flips which is which without
rewiring. audio_capture_read_stereo deinterleaves into primary+reference.
microSD pins (7/8/9) untouched. DSP + dual-VAD wired in the next task.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 4: Wire NLMS cancellation + dual VAD into `audio_task`; remove dead `vad_process`

**Files:**
- Modify: `firmware/sense_sensor/main/sense_sensor.c` (full pipeline rewire)
- Modify: `firmware/sense_sensor/main/vad.h` (remove `vad_process` declaration)
- Modify: `firmware/sense_sensor/main/vad.c` (remove `vad_process` definition)
- Modify: `firmware/sense_sensor/test/test_vad.c` (remove the single-channel `vad_process` tests; keep the dual-channel tests)

**Interfaces:**
- Consumes: `audio_capture_read_stereo` (Task 3), `vad_process_dual` + `vad_t.ratio_threshold` (Task 2), `mic_dsp_process` + `mic_dsp_t` (Task 1).
- Produces: a complete `audio_task` that emits cleaned mono Opus into the unchanged ring/§C.6/BLE path.

- [ ] **Step 1: Rewire `audio_task` in `sense_sensor.c`**

Add the include near the top (after `#include "vad.h"`):

```c
#include "mic_dsp.h"
```

Replace the buffer declarations and the loop body. The new `audio_task`:

```c
static void audio_task(void *arg) {
  (void)arg;
  static int16_t pri[FRAME_SAMPLES];
  static int16_t ref[FRAME_SAMPLES];
  static int16_t mono[FRAME_SAMPLES];
  static uint8_t opus_buf[MAX_OPUS_BYTES];
  vad_t vad;
  vad_init(&vad, VAD_ENERGY_THRESHOLD, VAD_HANGOVER_FRAMES);
  mic_dsp_t dsp;
  mic_dsp_init(&dsp);

  uint32_t frames = 0, voiced = 0, gaps = 0, total = 0;
  uint32_t rel_ts_ms = 0;
  uint32_t stack_report_at = 3000;
  for (;;) {
    if (audio_capture_read_stereo(pri, ref) != ESP_OK) {
      continue;  // DMA not ready yet; retry next tick
    }

    // VAD first: its decision gates both the encode and the NLMS adaptation.
    uint8_t state = vad_process_dual(&vad, pri, ref, FRAME_SAMPLES);
    // Adapt the noise canceller ONLY on noise-only frames; freeze during speech.
    bool adapt_now = (state == C6_GAP_MARKER);
    mic_dsp_process(&dsp, pri, ref, mono, FRAME_SAMPLES, adapt_now);

    if (state == C6_SPEECH || state == C6_HANGOVER) {
      // Voiced frame: encode the CLEANED mono and push the Opus packet.
      int n = opus_stream_encode(mono, opus_buf, sizeof opus_buf);
      if (n > 0 && n <= UINT8_MAX) {
        ring_buffer_push(state, rel_ts_ms, opus_buf, (uint8_t)n);
        voiced++;
        total += (uint32_t)n;
      } else {
        gaps++;  // encode error — treat as a gap so the server can backfill
      }
    } else {
      // Silence/noise: push a zero-length frame so the ring stays contiguous.
      ring_buffer_push(C6_GAP_MARKER, rel_ts_ms, NULL, 0);
      gaps++;
    }

    frames++;
    rel_ts_ms += FRAME_MS;

    if (frames == stack_report_at) {
      uint32_t hw = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
      ESP_LOGI(TAG, "audio stack high-water mark: %" PRIu32 " bytes free (stack size %" PRIu32 ")",
               hw, (uint32_t)32768);
      stack_report_at += 3000;
    }

    if (frames % (1000 / FRAME_MS) == 0) {  // ~once per second
      ESP_LOGI(TAG, "frames=%" PRIu32 " voiced=%" PRIu32 " gap=%" PRIu32 " opus_bytes/s=%" PRIu32,
               frames, voiced, gaps, total);
      total = 0;
    }
  }
}
```

Leave `app_main` as updated in Task 3 (the boot log already says "stereo (dual-mic)").

- [ ] **Step 2: Remove the dead single-channel `vad_process`**

In `vad.h`, delete the declaration:

```c
uint8_t vad_process(vad_t *vad, const int16_t *pcm, size_t n);
```

In `vad.c`, delete the `vad_process` function definition (lines 11-28 of the original).

- [ ] **Step 3: Remove the single-channel tests from `test/test_vad.c`**

Delete the block of `vad_process` calls in `main` (the original loud/quiet/hangover/re-onset/cold-start checks using the single-channel `vad_process`). Keep the `fill` helper (still used by the dual tests). Keep the dual-channel block from Task 2. The dual-channel block already covers hangover, cold-start, speech, and gap, so coverage is preserved.

- [ ] **Step 4: Run the host test suite**

Run: `cd firmware/sense_sensor/test && make`
Expected: vad (dual-channel only), dsp, c6, provisioning all pass; no link errors from a missing `vad_process`.

- [ ] **Step 5: Build the firmware**

Run: `cd firmware/sense_sensor && idf.py build`
Expected: succeeds. (Watch for an unused-variable warning on `ref` — it is now used by `vad_process_dual` and `mic_dsp_process`, so the `(void)ref;` line from Task 3 must be removed. Delete it.)

- [ ] **Step 6: On-device smoke (requires hardware)**

Flash + monitor. Confirm:
- Boot log and per-second line look sane.
- In a noisy room: `gap` count is high (ambient rejected by the ratio gate), `voiced` rises only when you speak toward the front mic.
- Speak quietly toward the front mic while ambient plays toward the back mic: `voiced` should track your speech with fewer interruptions than the old energy-only path.
- Cover the front mic: `voiced` drops (no voice on primary); ambient alone → `gap`.

If no device, note "smoke pending hardware" and proceed.

- [ ] **Step 7: Commit**

```bash
git add firmware/sense_sensor/main/sense_sensor.c firmware/sense_sensor/main/vad.h \
        firmware/sense_sensor/main/vad.c firmware/sense_sensor/test/test_vad.c
git commit -m "feat(firmware): wire dual-mic NLMS + ratio VAD into audio_task

audio_task now: read_stereo -> vad_process_dual -> mic_dsp_process (adapt
only on noise-only frames) -> opus encode the cleaned mono. Loud correlated
ambient is rejected by the ratio VAD; correlated noise is subtracted by the
NLMS canceller. Clean mono feeds the unchanged Opus/ring/§C.6/BLE path.
Removes the dead single-channel vad_process and its tests.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

### Task 5: Hardware tuning, docs, and memory

**Files:**
- Modify: `firmware/sense_sensor/main/config.h` (finalize tunable comments after calibration)
- Modify: `firmware/sense_sensor/README.md` (pin map + L/R wiring + tuning procedure)
- Modify: memory file (record the dual-mic setup)

**Interfaces:** none (documentation + calibration).

- [ ] **Step 1: Add a calibration log to `audio_task` (temporary)**

Add a once-per-second debug line that prints primary/reference energy and ratio so the thresholds can be tuned on hardware. Inside the existing `if (frames % (1000 / FRAME_MS) == 0)` block in `audio_task`, compute (cheap, over the last frame is fine — or omit and just print `voiced/gap` which already reflects the ratio gate):

```c
      // Temporary calibration aid: primary vs reference energy of the last frame.
      uint64_t ep = 0, er = 0;
      for (int i = 0; i < FRAME_SAMPLES; i++) { ep += (uint64_t)pri[i]*pri[i]; er += (uint64_t)ref[i]*ref[i]; }
      ESP_LOGI(TAG, "cal e_pri=%llu e_ref=%llu ratio=%llu",
               (unsigned long long)(ep/FRAME_SAMPLES),
               (unsigned long long)(er/FRAME_SAMPLES),
               (unsigned long long)((ep/FRAME_SAMPLES)/((er/FRAME_SAMPLES)+1)));
```

- [ ] **Step 2: Calibrate on hardware (requires device)**

Flash + monitor. Capture `cal` lines in three conditions: silence, ambient noise (both mics), speech toward the front mic. Pick:
- `VAD_ENERGY_THRESHOLD` just above the silence floor.
- `VAD_RATIO_THRESHOLD` between the ambient ratio (~1) and the speech ratio (typically 4-10×). Default 4 is usually safe.
- `DSP_NLMS_STEP_Q15`: if cancellation is weak, raise toward 9830 (0.3); if it rings/instable, lower toward 3276 (0.1).
- `DSP_NLMS_TAPS`: 32 is fine for front/back spacing of a few cm; raise to 64 only if the path mismatch is longer.

Update `config.h` comments from "TUNE ON HARDWARE" to the calibrated values (or leave the defaults and record the calibrated values in the README).

- [ ] **Step 3: Document the array in `README.md`**

Add a "Dual IENMP441 microphone array" section: pin map table (SCK=GPIO4/D3, WS=GPIO5/D4, SD=GPIO6/D5, L/R→GND/VDD, VDD→3V3, GND), the L/R channel-select wiring, `PRIMARY_CHANNEL` flip, the DSP/VAD approach in one paragraph, and the tuning procedure from Step 2. Note that microSD (7/8/9/21) is preserved and the old PDM mic (41/42) is removed.

- [ ] **Step 4: Remove the temporary calibration log**

Delete the `cal` log block added in Step 1 (keep the existing per-second `voiced/gap/opus_bytes` line).

- [ ] **Step 5: Final build + host tests**

Run: `cd firmware/sense_sensor && idf.py build` and `cd firmware/sense_sensor/test && make`
Expected: both green.

- [ ] **Step 6: Record a memory note**

Write `/Users/kevin/.claude/projects/-Users-kevin-Projects-Sense/memory/dual-mic-array.md` (type: project) recording: dual IENMP441 on shared I2S bus GPIO 4/5/6, NLMS + ratio-VAD approach, uplink unchanged, esp-nsk deferred, tunables calibrated values. Add a one-line pointer to `MEMORY.md`.

- [ ] **Step 7: Commit**

```bash
git add firmware/sense_sensor/README.md firmware/sense_sensor/main/config.h \
        firmware/sense_sensor/main/sense_sensor.c
git commit -m "docs(firmware): dual IENMP441 array pinout, wiring, and tuning notes

Records the shared-bus I2S wiring (GPIO 4/5/6), L/R channel-select, the
NLMS+ratio-VAD approach, and the hardware tuning procedure. esp-nsk deferred.

Co-Authored-By: Claude <noreply@anthropic.com>"
```

---

## Self-Review

**Spec coverage:**
- §2 architecture (capture → DSP → dual-VAD → opus → ring unchanged): Tasks 1-4.
- §3.1 audio_capture I2S std stereo: Task 3.
- §3.2 mic_dsp NLMS (adapt freeze, Q15, taps/step/leak/eps): Task 1.
- §3.3 dual-channel ratio VAD + graceful fallback: Task 2.
- §3.4 config.h changes (add I2S/DSP/ratio defines, remove PDM, keep CHANNELS=1): Tasks 1-3.
- §3.5 L/R wiring + PRIMARY_CHANNEL: Task 3 (config) + Task 5 (docs).
- §4 pin map (GPIO 4/5/6, SD preserved): Task 3 + Task 5.
- §5 error handling (short read → skip + advance rel_ts, saturation clamp, reference-dead fallback, PSRAM discipline): Task 1 (clamp/fallback/stability tests), Task 4 (skip+advance preserved from existing code), config/PSRAM untouched.
- §6 testing (mic_dsp host tests, vad dual host tests, hardware smoke): Tasks 1, 2, 5.
- §7 out of scope (no contract change, no esp-nsk, no SD change, no BLE change): enforced by Global Constraints + the file lists (ble_drain/c6_packet/ble_link/opus_stream/ring_buffer never modified).

**Placeholder scan:** no TBD/TODO/"handle edge cases" — every code step has full code. Tunable values are concrete (with TUNE-ON-HARDWARE labels and a calibration task).

**Type consistency:** `mic_dsp_t`, `mic_dsp_init`, `mic_dsp_process(... bool adapt_now)` — same in Task 1 (definition) and Task 4 (use). `vad_process_dual(vad_t*, const int16_t*, const int16_t*, size_t)` — same in Task 2 (definition) and Task 4 (use). `audio_capture_read_stereo(int16_t*, int16_t*)` — same in Task 3 (definition) and Task 4 (use). `vad_t.ratio_threshold` added in Task 2, read in Task 2's `vad_process_dual`. `vad_init` signature unchanged so Task 3's interim `audio_task` compiles. `vad_process` removed in Task 4 only after all callers switch.

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-07-26-dual-mic-voice-isolation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

Which approach?