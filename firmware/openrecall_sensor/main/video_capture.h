/*
 * P4b continuous video — start/stop/record MJPEG clips to SD.
 *
 * video_start opens one .mjpeg clip file (MJPEG = concatenation of JPEGs; the
 * server's split_mjpeg splits on SOI boundaries) and spawns a core-0 task that
 * captures JPEG frames at ~VIDEO_FPS, fwrites them into the kept-open file, and
 * flushes roughly every 1 s to bound PSRAM burst memory. video_stop signals the
 * task to finish its last frame, flush+close the file, append the manifest line
 * (kind 'v'), and only then return — so callers (and the transfer HTTP server)
 * never see a half-written clip. video_record is the record_video handler: it
 * runs start->delay(duration)->stop on a short-lived task so it does not block
 * the executor task that dispatched it.
 */
#pragma once
#include "esp_err.h"
#include <stdbool.h>
#include <stdint.h>

esp_err_t video_start(uint32_t rel_ts_ms);   /* start_video handler body */
void      video_stop(void);                   /* stop_video handler body */
esp_err_t video_record(uint32_t duration_s, uint32_t rel_ts_ms); /* record_video */
bool      video_is_recording(void);