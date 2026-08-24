/*
 * P4b ambient snapshot — a FreeRTOS software timer that fires every
 * SNAPSHOT_INTERVAL_S, captures a JPEG, writes it to SD, and appends a
 * manifest line. Also exposes rel_ts_ms_now(): the device's monotonic ms
 * clock (owned by the audio task) so snapshots/videos stamp the same timeline
 * as the §C.6 audio packets.
 */
#pragma once
#include "esp_err.h"
#include <stdint.h>

esp_err_t snapshot_init(void);             /* create + start the timer at SNAPSHOT_INTERVAL_S */
void      snapshot_set_interval(uint32_t seconds);  /* 0 = stop; >0 = new period + start */
esp_err_t snapshot_capture_one(uint32_t rel_ts_ms); /* capture_photo handler body */
uint32_t  rel_ts_ms_now(void);            /* monotonic ms clock (shared with §C.6) */