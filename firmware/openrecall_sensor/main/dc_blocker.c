#include "dc_blocker.h"

/* R = 0.995 in Q15 (0.995 * 32768 = 32604). */
#define DC_BLOCKER_R_Q15 32604

static inline int16_t sat_int16(int32_t v) {
  if (v > 32767)  return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void dc_blocker_init(dc_blocker_t *b) {
  b->x_prev = 0;
  b->y_prev = 0;
}

void dc_blocker_process(dc_blocker_t *b, int16_t *pcm, size_t n) {
  int16_t x_prev = b->x_prev;
  int16_t y_prev = b->y_prev;
  for (size_t i = 0; i < n; i++) {
    int16_t x = pcm[i];
    /* y[n] = x[n] - x[n-1] + R * y[n-1]
     * R * y_prev is Q15 * Q0 -> Q15; shift back to Q0. */
    int32_t y = (int32_t)x - (int32_t)x_prev
              + (int32_t)((DC_BLOCKER_R_Q15 * (int32_t)y_prev) >> 15);
    int16_t y16 = sat_int16(y);
    pcm[i] = y16;
    x_prev = x;
    y_prev = y16;
  }
  b->x_prev = x_prev;
  b->y_prev = y_prev;
}