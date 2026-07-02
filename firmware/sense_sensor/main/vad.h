/*
 * Software VAD — gates 24/7 audio so the server isn't fed constant silence.
 *
 * Energy-based classifier + hangover state machine. Per 20 ms frame it returns the
 * §C.6 vad_state to tag:
 *   - C6_SPEECH      : frame is voiced
 *   - C6_HANGOVER    : recently voiced; keep emitting (avoids clipping word tails)
 *   - C6_GAP_MARKER  : silence; suppress audio, emit a gap marker instead
 *
 * Pre-roll (emitting the ~300 ms BEFORE onset) is NOT done here: every frame is in
 * the ring buffer anyway, so the streaming layer replays pre-onset frames from
 * history. That keeps this module pure and host-testable (no buffering, no ESP deps).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint32_t energy_threshold;  // per-sample mean-square gate
  int      hangover_frames;   // configured hangover length
  int      hangover_left;     // remaining hangover countdown
} vad_t;

// Initialise with an energy threshold and hangover length (in frames).
void vad_init(vad_t *vad, uint32_t energy_threshold, int hangover_frames);

// Classify one frame of `n` int16 samples; returns a c6_vad_state value.
uint8_t vad_process(vad_t *vad, const int16_t *pcm, size_t n);

#ifdef __cplusplus
}
#endif
