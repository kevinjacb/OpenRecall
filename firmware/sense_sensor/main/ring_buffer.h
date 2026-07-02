/*
 * Encoded-audio ring buffer — 60 s of Opus frames + metadata in PSRAM.
 *
 * The producer (core-1 audio task) pushes one encoded frame per 20 ms. The consumer
 * (core-0 BLE task) keeps its own monotonic cursor and reads forward; on speech
 * onset it rewinds its cursor by VAD_PREROLL_FRAMES to replay pre-onset history.
 * Frame indices are monotonic (total-ever-written), so "the last N frames" and
 * "frames since cursor" are just arithmetic; the backing store is a fixed circular
 * array of fixed-size slots (simple, lock-cheap; PSRAM is plentiful).
 */
#pragma once

#include "esp_err.h"

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

typedef struct {
  uint8_t        vad_state;
  uint32_t       rel_ts_ms;
  uint8_t        len;
  const uint8_t *data;  // points into the ring; valid until overwritten (~60 s)
} ring_frame_t;

// Allocate the ring in PSRAM. Call once at startup.
esp_err_t ring_buffer_init(void);

// Push one encoded frame (copies up to MAX_OPUS_BYTES). Overwrites the oldest frame
// when full. `len == 0` is valid (a gap marker carries no audio).
void ring_buffer_push(uint8_t vad_state, uint32_t rel_ts_ms, const uint8_t *data, uint8_t len);

// Total frames ever written (also the index just past the newest frame).
uint32_t ring_buffer_write_index(void);

// Copy the frame at absolute `index` into `out`. Returns false if that index has
// already been overwritten or hasn't been written yet.
bool ring_buffer_get(uint32_t index, ring_frame_t *out);

#ifdef __cplusplus
}
#endif
