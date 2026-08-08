/*
 * Audio capture — I2S standard stereo RX from the dual IENMP441 array via DMA.
 *
 * Two MEMS mics on a shared I2S bus (SCK/WS/SD); L/R channel-select splits them
 * into left (primary/voice) and right (reference/ambient). Thin wrapper over the
 * IDF I2S driver: stereo RX channel with DMA buffers sized to one 20 ms frame
 * per channel, deinterleaved into primary + reference. The audio task (core 1)
 * calls read_stereo() in a loop.
 */
#pragma once

#include "config.h"
#include "esp_err.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Configure and enable the I2S std stereo RX channel. Call once at startup.
esp_err_t audio_capture_init(void);

// Block until one full 20 ms stereo frame is read and deinterleaved into
// `primary` (voice mic) and `reference` (ambient mic). Each must hold
// FRAME_SAMPLES int16 samples. Returns ESP_ERR_INVALID_SIZE on a short read.
esp_err_t audio_capture_read_stereo(int16_t *primary, int16_t *reference);

#ifdef __cplusplus
}
#endif