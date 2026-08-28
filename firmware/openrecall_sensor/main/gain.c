#include "gain.h"

static inline int16_t sat_int16(int32_t v) {
  if (v > 32767)  return 32767;
  if (v < -32768) return -32768;
  return (int16_t)v;
}

void gain_apply(int16_t *pcm, size_t n, uint32_t gain_q8) {
  for (size_t i = 0; i < n; i++) {
    int32_t y = ((int32_t)pcm[i] * (int32_t)gain_q8) >> 8;
    pcm[i] = sat_int16(y);
  }
}