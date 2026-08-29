#include "hpf.h"

#include "config.h"

static inline int16_t sat_int16(int32_t v) {
  if (v > 32767)  return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void hpf_init(hpf_t *f) {
  f->x1 = 0;
  f->x2 = 0;
  f->y1_q14 = 0;
  f->y2_q14 = 0;
}

void hpf_process(hpf_t *f, int16_t *pcm, size_t n) {
  int32_t x1 = f->x1, x2 = f->x2;
  int32_t y1 = f->y1_q14, y2 = f->y2_q14;
  for (size_t i = 0; i < n; i++) {
    int32_t x0 = pcm[i];
    /* Direct Form I:
     *   y[n] = b0 x[n] + b1 x[n-1] + b2 x[n-2] - a1 y[n-1] - a2 y[n-2]
     * b*x is Q14; shift up to Q28 to match a(Q14) * y(Q14). The int64
     * accumulator makes intermediate overflow impossible (|acc| < 2^46). */
    int64_t acc = ((int64_t)HPF_B0_Q14 * x0
                 + (int64_t)HPF_B1_Q14 * x1
                 + (int64_t)HPF_B2_Q14 * x2) << 14;
    acc -= (int64_t)HPF_A1_Q14 * y1;
    acc -= (int64_t)HPF_A2_Q14 * y2;
    int32_t y0 = (int32_t)(acc >> 14);          /* back to Q14 */
    pcm[i] = sat_int16((y0 + (1 << 13)) >> 14); /* Q14 -> Q0, rounded */
    x2 = x1;
    x1 = x0;
    y2 = y1;
    y1 = y0;
  }
  f->x1 = x1;
  f->x2 = x2;
  f->y1_q14 = y1;
  f->y2_q14 = y2;
}
