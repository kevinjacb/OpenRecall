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
  uint32_t energy_threshold;  // per-sample mean-square gate on primary
  uint32_t ratio_threshold;   // primary/ref energy ratio gate (e_pri > ratio*(e_ref+1))
  int      hangover_frames;   // configured hangover length
  int      hangover_left;     // remaining hangover countdown
} vad_t;

// Initialise with an energy threshold and hangover length (in frames).
// ratio_threshold is populated from VAD_RATIO_THRESHOLD.
void vad_init(vad_t *vad, uint32_t energy_threshold, int hangover_frames);

// Dual-channel classifier: speech iff primary energy > energy_threshold AND
// primary energy > ratio_threshold * (reference energy + 1). Same hangover
// state machine as the original single-channel VAD. Returns a c6_vad_state.
uint8_t vad_process_dual(vad_t *vad, const int16_t *primary, const int16_t *reference, size_t n);

// Single-channel classifier: speech iff primary energy > energy_threshold,
// with the same hangover state machine. Use when the reference mic carries as
// much voice as the primary (two omnidirectional mics with insufficient acoustic
// shadowing -> ratio ~1 -> the dual ratio gate would reject the voice). The
// reference channel is ignored entirely. Returns a c6_vad_state.
uint8_t vad_process_single(vad_t *vad, const int16_t *primary, size_t n);

#ifdef __cplusplus
}
#endif
