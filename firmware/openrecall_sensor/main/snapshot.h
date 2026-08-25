/*
 * P4b ambient snapshot — a FreeRTOS software timer that fires every
 * SNAPSHOT_INTERVAL_S and signals a dedicated worker task (8 KB stack) which
 * captures a JPEG, writes it to SD, and appends a manifest line. The capture
 * runs on the WORKER, not the timer service task: esp32-camera capture + SD
 * write is a deep call chain that overflows the 2 KB Tmr Svc stack, and a ~1 s
 * blocking capture has no business on the shared timer task. snapshot_capture_
 * async() signals the same worker — used by both the timer and the executor's
 * CMD_CAPTURE_PHOTO handler so camera capture never runs on a small-stack task.
 * Also exposes rel_ts_ms_now(): the device's monotonic ms clock (owned by the
 * audio task) so snapshots/videos stamp the same timeline as the §C.6 packets.
 */
#pragma once
#include "esp_err.h"
#include <stdint.h>

esp_err_t snapshot_init(void);             /* create the worker + start the timer at SNAPSHOT_INTERVAL_S */
void      snapshot_set_interval(uint32_t seconds);  /* 0 = stop; >0 = new period + start */
esp_err_t snapshot_capture_one(uint32_t rel_ts_ms); /* synchronous capture (runs on the worker) */
void      snapshot_capture_async(void);    /* signal the worker to capture one now (timer + executor use this) */
uint32_t  rel_ts_ms_now(void);            /* monotonic ms clock (shared with §C.6) */