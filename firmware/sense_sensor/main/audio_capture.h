/*
 * Audio capture — I2S PDM RX from the onboard mic via DMA (16 kHz mono).
 *
 * Thin wrapper over the IDF I2S driver: set up a PDM-RX channel with DMA buffers
 * sized to one 20 ms frame, then read whole frames. The audio task (core 1) calls
 * read_frame() in a loop; DMA means the CPU only copies, it doesn't bit-bang.
 */
#pragma once

#include "config.h"
#include "esp_err.h"

#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

// Configure and enable the PDM RX channel. Idempotent-safe to call once at startup.
esp_err_t audio_capture_init(void);

// Block until one full 20 ms frame (FRAME_SAMPLES int16 samples) is read into `out`.
// `out` must hold at least FRAME_SAMPLES samples.
esp_err_t audio_capture_read_frame(int16_t *out);

#ifdef __cplusplus
}
#endif
