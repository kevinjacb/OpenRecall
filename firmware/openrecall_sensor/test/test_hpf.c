/*
 * Host-side test for the wind-cut high-pass biquad (no ESP toolchain).
 *   cc -std=c11 -I../main ../main/hpf.c test_hpf.c -o /tmp/hpftest && /tmp/hpftest
 *
 * Feeds int16 sine waves through the filter in FRAME_SAMPLES chunks (the same
 * framing the audio task uses) and measures RMS attenuation after settling.
 * Expected (fc = 180 Hz Butterworth @ 16 kHz, Q14-quantized):
 *   DC      -> essentially zero
 *   50 Hz   -> >= 18 dB down (theory 22.3 dB)
 *   100 Hz  -> >=  8 dB down (theory 10.6 dB)
 *   500 Hz  -> within 1.5 dB of unity
 *   1 kHz   -> within 1.0 dB of unity
 */
#include "config.h"
#include "hpf.h"

#include <math.h>
#include <stdio.h>
#include <stdint.h>

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* Run `seconds` worth of a sine at `hz` (amp 8000) through the filter in
 * frame-sized chunks; return output/input RMS ratio in dB, measured over the
 * second half (first half discarded as settling time). hz==0 -> DC step. */
static double run_tone_db(double hz, double seconds) {
  hpf_t f;
  hpf_init(&f);
  const double amp = 8000.0;
  const int total = (int)(16000 * seconds);
  int16_t buf[FRAME_SAMPLES];
  double in_sq = 0.0, out_sq = 0.0;
  int counted = 0;
  for (int start = 0; start + FRAME_SAMPLES <= total; start += FRAME_SAMPLES) {
    double in_frame[FRAME_SAMPLES];
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      double t = (double)(start + i) / 16000.0;
      double v = (hz == 0.0) ? amp : amp * sin(2.0 * M_PI * hz * t);
      in_frame[i] = v;
      buf[i] = (int16_t)lrint(v);
    }
    hpf_process(&f, buf, FRAME_SAMPLES);
    if (start >= total / 2) {
      for (int i = 0; i < FRAME_SAMPLES; i++) {
        in_sq += in_frame[i] * in_frame[i];
        out_sq += (double)buf[i] * (double)buf[i];
        counted++;
      }
    }
  }
  if (counted == 0 || in_sq <= 0.0) return -999.0;
  double ratio = sqrt(out_sq / in_sq);
  return 20.0 * log10(ratio > 1e-9 ? ratio : 1e-9);
}

static void test_dc_is_removed(void) {
  double db = run_tone_db(0.0, 1.0);
  printf("  DC: %.1f dB\n", db);
  check("DC step suppressed (<= -40 dB)", db <= -40.0);
}

static void test_50hz_wind_band_cut(void) {
  double db = run_tone_db(50.0, 1.0);
  printf("  50 Hz: %.1f dB\n", db);
  check("50 Hz cut >= 18 dB", db <= -18.0);
}

static void test_100hz_rumble_cut(void) {
  double db = run_tone_db(100.0, 1.0);
  printf("  100 Hz: %.1f dB\n", db);
  check("100 Hz cut >= 8 dB", db <= -8.0);
}

static void test_500hz_speech_band_passthrough(void) {
  double db = run_tone_db(500.0, 1.0);
  printf("  500 Hz: %.2f dB\n", db);
  check("500 Hz within 1.5 dB of unity", db >= -1.5 && db <= 0.5);
}

static void test_1khz_speech_band_passthrough(void) {
  double db = run_tone_db(1000.0, 1.0);
  printf("  1 kHz: %.2f dB\n", db);
  check("1 kHz within 1.0 dB of unity", db >= -1.0 && db <= 0.5);
}

static void test_state_continuity_across_frames(void) {
  /* Processing one long buffer must equal processing the same samples in
   * frame-sized chunks — the state carries across the boundary. */
  enum { N = FRAME_SAMPLES * 4 };
  int16_t whole[N], chunked[N];
  for (int i = 0; i < N; i++) {
    double v = 6000.0 * sin(2.0 * M_PI * 700.0 * i / 16000.0);
    whole[i] = chunked[i] = (int16_t)lrint(v);
  }
  hpf_t a, b;
  hpf_init(&a);
  hpf_init(&b);
  hpf_process(&a, whole, N);
  for (int s = 0; s < N; s += FRAME_SAMPLES) {
    hpf_process(&b, chunked + s, FRAME_SAMPLES);
  }
  int same = 1;
  for (int i = 0; i < N; i++) {
    if (whole[i] != chunked[i]) { same = 0; break; }
  }
  check("chunked == whole (state continuous across frames)", same);
}

static void test_silence_stays_silent(void) {
  int16_t buf[FRAME_SAMPLES] = {0};
  hpf_t f;
  hpf_init(&f);
  hpf_process(&f, buf, FRAME_SAMPLES);
  int all_zero = 1;
  for (int i = 0; i < FRAME_SAMPLES; i++) {
    if (buf[i] != 0) { all_zero = 0; break; }
  }
  check("zero in -> zero out", all_zero);
}

int main(void) {
  printf("hpf host tests\n");
  test_dc_is_removed();
  test_50hz_wind_band_cut();
  test_100hz_rumble_cut();
  test_500hz_speech_band_passthrough();
  test_1khz_speech_band_passthrough();
  test_state_continuity_across_frames();
  test_silence_stays_silent();
  printf(failures ? "FAILURES: %d\n" : "all pass\n", failures);
  return failures ? 1 : 0;
}
