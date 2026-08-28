/*
 * Fixed-point input gain — lift the mic signal before Opus encode.
 *
 * The INMP441 MEMS mic has no AGC and produces a quiet signal at wearable
 * distance (~1.4% full scale). A fixed Q8 multiply gives Opus and the
 * downstream ASR (Whisper/Parakeet) a hotter signal, so the encoder's bits go
 * to voice rather than to near-silence. Hard-saturates at the int16 rails on
 * rare full-scale peaks (matches the dc_blocker / mic_dsp saturation idiom).
 *
 * Pure C / no ESP deps / fixed-point Q8, so it builds under the same host
 * contract test as vad.c and dc_blocker.c. In-place, stateless.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Apply a Q8 fixed-point gain in place: pcm[i] = sat_int16(pcm[i] * gain_q8 >> 8).
 * Stateless — safe to call every voiced frame. gain_q8 = 256 is unity (x1),
 * 2048 is x8. */
void gain_apply(int16_t *pcm, size_t n, uint32_t gain_q8);

#ifdef __cplusplus
}
#endif