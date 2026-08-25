#pragma once
#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

esp_err_t camera_init(void);
/* Capture one JPEG. *out_buf is heap-allocated (caller frees); *out_len its length. */
esp_err_t camera_capture_jpeg(uint8_t **out_buf, size_t *out_len);
/* Drain a stale frame from the on-demand buffer so the next camera_capture_jpeg
 * returns a fresh image. Only the ambient snapshot path needs this (long idle
 * between captures); the video loop keeps the pipeline fresh. */
void camera_flush_stale(void);