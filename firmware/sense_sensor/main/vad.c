#include "vad.h"

#include "config.h"

void vad_init(vad_t *vad, uint32_t energy_threshold, int hangover_frames) {
  vad->energy_threshold = energy_threshold;
  vad->hangover_frames = hangover_frames;
  vad->hangover_left = 0;
}

uint8_t vad_process(vad_t *vad, const int16_t *pcm, size_t n) {
  uint64_t sum_sq = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t s = pcm[i];
    sum_sq += (uint64_t)(s * s);
  }
  uint32_t mean_sq = (n > 0) ? (uint32_t)(sum_sq / n) : 0;

  if (mean_sq > vad->energy_threshold) {
    vad->hangover_left = vad->hangover_frames;  // re-arm on every voiced frame
    return C6_SPEECH;
  }
  if (vad->hangover_left > 0) {
    vad->hangover_left--;
    return C6_HANGOVER;
  }
  return C6_GAP_MARKER;
}
