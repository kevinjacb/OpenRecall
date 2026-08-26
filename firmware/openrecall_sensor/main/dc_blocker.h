/*
 * DC blocker — one-pole high-pass filter on the captured mic PCM.
 *
 * The INMP441 MEMS mic has no AGC and no hardware high-pass, so a DC bias
 * accumulates in the captured signal (and drifts with temperature / handling).
 * A DC offset costs ~3 dB of effective headroom and, worse, is energy the Opus
 * VOIP encoder spends bits encoding — bits that then do not go to voice. It
 * also biases the energy VAD: a steady DC offset looks like a steady signal.
 * Blocking DC before encode is the cheapest win available on the raw path.
 *
 * This is the standard DC-blocking filter (Speex / audio effect textbook):
 *
 *     y[n] = x[n] - x[n-1] + R * y[n-1],   R = 0.995
 *
 * The differencer kills any constant (DC has zero derivative), and the pole at
 * +R gently restores low-frequency gain so the voice band (>=~80 Hz) is passed
 * with negligible attenuation. R = 0.995 places the cutoff near 16 Hz at
 * 16 kHz, well below speech. Pure C / no ESP deps / fixed-point Q15, so it
 * builds under the same host contract test as vad.c and mic_dsp.c.
 *
 * Single-mic, in-place, stateful across frames (x_prev / y_prev persist for the
 * life of the audio task). Not the dual-mic NLMS canceller in mic_dsp.c — this
 * runs unconditionally on the primary mic whether or not the canceller is ever
 * re-enabled; the two are independent.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int16_t x_prev;  /* last input sample (x[n-1]) */
  int16_t y_prev;  /* last output sample (y[n-1]) */
} dc_blocker_t;

/* Zero the filter state (start as a passthrough at DC = 0). */
void dc_blocker_init(dc_blocker_t *b);

/* Remove DC from `n` int16 samples in place. State persists across calls so
 * the filter is continuous across the frame boundary. */
void dc_blocker_process(dc_blocker_t *b, int16_t *pcm, size_t n);

#ifdef __cplusplus
}
#endif