/*
 * P4b SD store — microSD over SPI (sdspi + FatFS) on the reserved pins.
 * Mounted lazily on first capture/transfer. Burst-writes only (the caller
 * stages frames in PSRAM first — Spike-2 measured a 92 ms worst-case SD stall).
 */
#pragma once
#include "esp_err.h"
#include <stddef.h>
#include <stdint.h>

esp_err_t sd_store_mount(void);                                   /* idempotent */
esp_err_t sd_store_write(const char *path, const uint8_t *data, size_t len);
esp_err_t sd_store_append(const char *path, const char *line);
esp_err_t sd_store_read(const char *path, uint8_t *buf, size_t cap, size_t *out_len);
esp_err_t sd_store_read_manifest(char *buf, size_t cap, size_t *out_len);
esp_err_t sd_store_delete(const char *path);