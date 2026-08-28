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

uint8_t vad_process_single(vad_t *vad, const int16_t *primary, size_t n) {
  uint64_t sp = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t p = primary[i];
    sp += (uint64_t)(p * p);
  }
  uint32_t e_pri = (n > 0) ? (uint32_t)(sp / n) : 0;
  int speech = (e_pri > vad->energy_threshold);

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

void vad_init_adaptive(vad_t *vad, uint32_t floor, uint32_t multiplier, int hangover_frames) {
  vad->energy_threshold = floor;   /* unused by the adaptive path; kept for struct parity */
  vad->ratio_threshold = VAD_RATIO_THRESHOLD;
  vad->hangover_frames = hangover_frames;
  vad->hangover_left = 0;
  vad->noise_floor = floor;        /* seed: start at the absolute floor */
  vad->multiplier = multiplier;
  vad->floor_min = floor;
}

uint8_t vad_process_single_adaptive(vad_t *vad, const int16_t *primary, size_t n) {
  uint64_t sp = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t p = primary[i];
    sp += (uint64_t)(p * p);
  }
  uint32_t e = (n > 0) ? (uint32_t)(sp / n) : 0;

  /* Effective threshold: the higher of the absolute floor and the tracked
   * noise floor scaled by the multiplier. */
  uint32_t thresh = vad->noise_floor * vad->multiplier;
  if (thresh < vad->floor_min) thresh = vad->floor_min;

  if (e > thresh) {
    vad->hangover_left = vad->hangover_frames;  /* re-arm on every voiced frame */
    return C6_SPEECH;
  }
  if (vad->hangover_left > 0) {
    vad->hangover_left--;
    /* Floor is frozen during hangover so a word-tail doesn't pull it down. */
    return C6_HANGOVER;
  }

  /* GAP: update the noise-floor EMA (asymmetric: fast down, slow up). The
   * floor only moves on genuinely quiet frames, so speech never poisons it. */
  int64_t delta = (int64_t)e - (int64_t)vad->noise_floor;
  int64_t update = 0;
  if (delta < 0) {
    /* Frame quieter than floor: decay fast (alpha_down). */
    update = (delta * (int64_t)VAD_NOISE_ALPHA_DOWN_Q16) >> 16;
  } else if ((int64_t)e < (int64_t)vad->noise_floor * 3 / 2) {
    /* Frame louder than floor but within 1.5x: rise slow (alpha_up). A louder
     * burst (>1.5x floor) does NOT raise the floor — likely a stray syllable. */
    update = (delta * (int64_t)VAD_NOISE_ALPHA_UP_Q16) >> 16;
  }
  int64_t nf = (int64_t)vad->noise_floor + update;
  if (nf < 0) nf = 0;
  vad->noise_floor = (uint32_t)nf;

  return C6_GAP_MARKER;
}
