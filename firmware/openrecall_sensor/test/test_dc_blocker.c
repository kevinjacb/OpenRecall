/*
 * Host-side test for the DC blocker (no ESP toolchain).
 *   cc -std=c11 -I../main ../main/dc_blocker.c test_dc_blocker.c -o /tmp/dctest && /tmp/dctest
 */
#include "dc_blocker.h"
#include "config.h"

#include <stdio.h>
#include <string.h>

#define N FRAME_SAMPLES

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* A pure DC offset must be removed (the differencer kills any constant).
 * The filter is in-place, so each frame is refilled with the DC value — the
 * real firmware reads fresh PCM from the mic each frame; modelling that here
 * avoids feeding the filter its own output (a feedback loop). */
static void test_dc_offset_removed(void) {
  dc_blocker_t b;
  dc_blocker_init(&b);
  int16_t buf[N];
  for (int f = 0; f < 10; f++) {
    for (int i = 0; i < N; i++) buf[i] = 5000;  /* constant DC offset, fresh each frame */
    dc_blocker_process(&b, buf, N);
  }
  int32_t peak = 0;
  for (int i = 0; i < N; i++) {
    int32_t v = buf[i] < 0 ? -(int32_t)buf[i] : buf[i];
    if (v > peak) peak = v;
  }
  printf("  DC: peak after 10 frames = %d (input was 5000)\n", (int)peak);
  check("pure DC offset removed (peak < 100)", peak < 100);
}

/* An AC signal (voice stand-in) must pass through with little attenuation. */
static void test_voice_band_preserved(void) {
  dc_blocker_t b;
  dc_blocker_init(&b);
  int16_t in[N], out[N];
  /* 800 Hz sine at 16 kHz, amplitude 20000. 800 Hz >> 16 Hz cutoff -> ~unity gain. */
  for (int i = 0; i < N; i++) {
    /* Cheap integer sine: 1000 samples precomputed via LCG not available; use
     * a square wave instead (same band content, exact). */
    in[i] = (i / 10 % 2) ? 20000 : -20000;  /* 100 Hz square wave (period 160) */
  }
  memcpy(out, in, sizeof out);
  /* Prime the filter with a few frames so the transient settles. Each frame
   * is refilled from `in` — fresh mic PCM, not the filter's own output. */
  for (int f = 0; f < 5; f++) {
    memcpy(out, in, sizeof out);
    dc_blocker_process(&b, out, N);
  }
  memcpy(out, in, sizeof out);
  dc_blocker_process(&b, out, N);  /* measured frame */
  int32_t peak_in = 0, peak_out = 0;
  for (int i = 0; i < N; i++) {
    int32_t vi = in[i] < 0 ? -(int32_t)in[i] : in[i];
    int32_t vo = out[i] < 0 ? -(int32_t)out[i] : out[i];
    if (vi > peak_in) peak_in = vi;
    if (vo > peak_out) peak_out = vo;
  }
  printf("  voice: in peak=%d out peak=%d\n", (int)peak_in, (int)peak_out);
  /* A 100 Hz square wave is well above the 16 Hz cutoff; expect < 3 dB loss. */
  check("voice band preserved (out > 0.7 * in)", peak_out > (peak_in * 7) / 10);
}

/* State continuity: processing 2N samples as one call must give byte-identical
 * output to processing them as two N-sample calls — x_prev / y_prev carry
 * across the frame boundary exactly as a single longer call would. This is
 * the property the firmware relies on (per-frame calls, continuous filter). */
static void test_state_persists_across_frames(void) {
  int16_t one_shot[2 * N], two_shot[2 * N];
  for (int i = 0; i < 2 * N; i++) {
    /* A mix of DC + AC so both the differencer and the pole are exercised. */
    one_shot[i] = two_shot[i] = 4000 + (int16_t)((i / 10 % 2) ? 8000 : -8000);
  }
  dc_blocker_t a, b;
  dc_blocker_init(&a);
  dc_blocker_process(&a, one_shot, 2 * N);  /* single call */
  dc_blocker_init(&b);
  dc_blocker_process(&b, two_shot, N);       /* first frame */
  dc_blocker_process(&b, two_shot + N, N);  /* second frame */
  int ok = 1;
  for (int i = 0; i < 2 * N; i++) {
    if (one_shot[i] != two_shot[i]) {
      ok = 0;
      printf("  mismatch @%d: one_shot=%d two_shot=%d\n", i, one_shot[i], two_shot[i]);
      break;
    }
  }
  check("state persists: 2N as one call == two N calls", ok);
}

/* A zero signal stays zero (no self-noise). */
static void test_silence_is_silent(void) {
  dc_blocker_t b;
  dc_blocker_init(&b);
  int16_t buf[N];
  memset(buf, 0, sizeof buf);
  for (int f = 0; f < 5; f++) dc_blocker_process(&b, buf, N);
  for (int i = 0; i < N; i++) {
    if (buf[i] != 0) {
      check("silence stays zero", 0);
      return;
    }
  }
  check("silence stays zero", 1);
}

int main(void) {
  printf("dc_blocker:\n");
  test_dc_offset_removed();
  test_voice_band_preserved();
  test_state_persists_across_frames();
  test_silence_is_silent();
  if (failures) {
    printf("FAIL (%d)\n", failures);
    return 1;
  }
  printf("PASS\n");
  return 0;
}