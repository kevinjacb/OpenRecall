/*
 * Host-side test for the fixed-point gain stage (no ESP toolchain).
 *   cc -std=c11 -I../main ../main/gain.c test_gain.c -o /tmp/gaintest && /tmp/gaintest
 */
#include "config.h"
#include "gain.h"

#include <stdio.h>
#include <string.h>

#define N FRAME_SAMPLES

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

static void test_scales_signal(void) {
  int16_t buf[N];
  for (int i = 0; i < N; i++) buf[i] = 100;   /* amp 100 */
  gain_apply(buf, N, INPUT_GAIN_Q8);           /* x8 -> 800 */
  int32_t peak = 0;
  for (int i = 0; i < N; i++) {
    int32_t v = buf[i] < 0 ? -(int32_t)buf[i] : buf[i];
    if (v > peak) peak = v;
  }
  printf("  scale: in=100 out_peak=%d (expect ~800)\n", (int)peak);
  check("x8 gain scales 100 -> 800", peak >= 790 && peak <= 810);
}

static void test_saturates_loud_positive(void) {
  int16_t buf[N];
  for (int i = 0; i < N; i++) buf[i] = 20000;  /* amp 20000 * 8 = 160000 -> sat */
  gain_apply(buf, N, INPUT_GAIN_Q8);
  check("loud positive saturates at 32767", buf[0] == 32767);
}

static void test_saturates_loud_negative(void) {
  int16_t buf[N];
  for (int i = 0; i < N; i++) buf[i] = -20000;  /* * 8 = -160000 -> sat */
  gain_apply(buf, N, INPUT_GAIN_Q8);
  check("loud negative saturates at -32768", buf[0] == -32768);
}

static void test_zero_stays_zero(void) {
  int16_t buf[N];
  memset(buf, 0, sizeof buf);
  gain_apply(buf, N, INPUT_GAIN_Q8);
  int ok = 1;
  for (int i = 0; i < N; i++) if (buf[i] != 0) { ok = 0; break; }
  check("zero stays zero", ok);
}

static void test_unity_gain_is_passthrough(void) {
  int16_t buf[N];
  for (int i = 0; i < N; i++) buf[i] = (int16_t)(i % 2000 - 1000);
  int16_t snap[N];
  memcpy(snap, buf, sizeof snap);
  gain_apply(buf, N, 256u);   /* x1 in Q8 (1 << 8) */
  int ok = 1;
  for (int i = 0; i < N; i++) if (buf[i] != snap[i]) { ok = 0; break; }
  check("unity (x1, Q8=256) is passthrough", ok);
}

int main(void) {
  printf("gain:\n");
  test_scales_signal();
  test_saturates_loud_positive();
  test_saturates_loud_negative();
  test_zero_stays_zero();
  test_unity_gain_is_passthrough();
  if (failures) {
    printf("FAIL (%d)\n", failures);
    return 1;
  }
  printf("PASS\n");
  return 0;
}