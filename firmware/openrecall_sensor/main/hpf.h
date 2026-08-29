/*
 * hpf — 2nd-order Butterworth high-pass on the captured mic PCM (wind cut).
 *
 * The DC blocker's corner sits near 16 Hz, so wind buffeting, rumble and
 * handling noise (10-400 Hz, energy concentrated below 200 Hz) pass straight
 * through it and then (a) drive the energy VAD — a gust reads as speech and
 * inflates the adaptive noise floor until quiet speech is rejected, (b) get
 * multiplied by the pre-encode gain and clip into hard distortion, and
 * (c) burn Opus's bit budget on non-speech. This biquad cuts that band
 * BEFORE the VAD and the gain stage, so both operate on speech-band signal.
 *
 * fc ~= 180 Hz at 16 kHz (Q = 0.7071, Butterworth): -22 dB at 50 Hz,
 * -11 dB at 100 Hz, -0.1 dB at 500 Hz and above — male fundamentals near
 * 100-150 Hz lose some energy, but their harmonics (where ASR reads speech)
 * are untouched; telephone bandwidth starts at 300 Hz for the same reason.
 *
 * Direct Form I, Q14 coefficients (config.h HPF_*), int64 accumulator so no
 * intermediate overflow, output state kept in Q14 for feedback precision.
 * Pure C / no ESP deps — host-tested under test/test_hpf.c like dc_blocker.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  int32_t x1, x2;      /* previous inputs, Q0 */
  int32_t y1_q14, y2_q14; /* previous outputs, Q14 (kept wide for feedback) */
} hpf_t;

/* Zero the filter state (start as a passthrough at signal = 0). */
void hpf_init(hpf_t *f);

/* High-pass `n` int16 samples in place. State persists across calls so the
 * filter is continuous across the frame boundary. */
void hpf_process(hpf_t *f, int16_t *pcm, size_t n);

#ifdef __cplusplus
}
#endif
