/*
 * Dual-mic DSP — adaptive differential noise cancellation (pure C, host-testable).
 *
 * Widrow NLMS noise canceller in int32 Q15 fixed-point. The reference mic faces
 * ambient; the primary mic faces the voice. y[n] = primary[n] - sum_k w[k]*ref[n-k].
 * The filter w adapts only when adapt_now is true (noise-only frames, gated by the
 * VAD in the caller); it is frozen during speech so voice is never cancelled.
 *
 * No ESP dependencies: only config.h + standard C, so it builds under the host
 * contract test the same way as vad.c / c6_packet.c.
 */
#pragma once

#include "config.h"

#include <stddef.h>
#include <stdint.h>
#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int32_t w[DSP_NLMS_TAPS];      /* Q15 filter coeffs (reference -> primary noise path) */
  int16_t delay[DSP_NLMS_TAPS];  /* reference sample delay ring */
  int      didx;                 /* delay ring write index */
} mic_dsp_t;

/* Zero the filter and delay line. */
void mic_dsp_init(mic_dsp_t *d);

/* Process one frame of `n` samples. out gets the cleaned mono (Q0 int16).
 * When adapt_now, the filter is NLMS-updated from the residual (call this only
 * on noise-only frames). When false, w is held and the last-learned noise path
 * is applied. `primary`, `reference`, `out` each hold `n` int16 samples. */
void mic_dsp_process(mic_dsp_t *d, const int16_t *primary, const int16_t *reference,
                     int16_t *out, size_t n, bool adapt_now);

#ifdef __cplusplus
}
#endif