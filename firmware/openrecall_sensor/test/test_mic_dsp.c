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
    gen_voice(voice, 100);   /* quiet voice buried in loud noise (SNR ~ -25 dB) */
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