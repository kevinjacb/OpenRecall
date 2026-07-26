#include "vad.h"

#include "config.h"

void vad_init(vad_t *vad, uint32_t energy_threshold, int hangover_frames) {
  vad->energy_threshold = energy_threshold;
  vad->ratio_threshold = VAD_RATIO_THRESHOLD;
  vad->hangover_frames = hangover_frames;
  vad->hangover_left = 0;
}

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
    vad->hangover_left = vad->hangover_frames;  // re-arm on every voiced frame
    return C6_SPEECH;
  }
  if (vad->hangover_left > 0) {
    vad->hangover_left--;
    return C6_HANGOVER;
  }
  return C6_GAP_MARKER;
}
