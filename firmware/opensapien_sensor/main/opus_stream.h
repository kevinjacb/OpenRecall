/*
 * Opus encoder — 16 kHz mono, 24 kbps, 20 ms, complexity 1 (all from Spike 1).
 *
 * Wraps the standard libopus C API (opus.h). libopus is not a first-party IDF
 * component; add it on the bench before enabling this module (see opus_stream.c
 * and the README). The encoder runs on core 1 (Spike 1: ~6 ms/frame, ~30% of one
 * core at complexity 1).
 */
#pragma once

#include "config.h"
#include "esp_err.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Create + configure the encoder (bitrate/complexity/voice signal from config.h).
esp_err_t opus_stream_init(void);

// Encode one 20 ms frame (FRAME_SAMPLES int16) into `out` (capacity `cap`).
// Returns encoded byte count (>0), or -1 on error.
int opus_stream_encode(const int16_t *pcm, uint8_t *out, int cap);

#ifdef __cplusplus
}
#endif
