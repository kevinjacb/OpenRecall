#pragma once
#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

esp_err_t camera_init(void);
/* Capture one JPEG. *out_buf is heap-allocated (caller frees); *out_len its length. */
esp_err_t camera_capture_jpeg(uint8_t **out_buf, size_t *out_len);