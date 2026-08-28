/*
 * Battery monitor — D0/GPIO1 voltage divider (100k/100k, x2.0) (spec 3.5).
 *
 * Ported from the selftest test_battery.c (commit 77fda41). ADC1_CH0 oneshot
 * + curve-fitting calibration, 64-sample oversample (drop min/max). ADC1 is
 * safe alongside WiFi (ADC2 is NOT — WiFi steals it). A low-priority task
 * samples every ~30 s and caches the latest reading for the Phase 4 status
 * characteristic and low-battery behavior.
 */
#pragma once

#include "esp_err.h"
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Init ADC1_CH0 oneshot + curve-fitting cali. Call once from app_main.
 * Returns ESP_OK on success. Non-fatal if it fails (device still boots). */
esp_err_t battery_init(void);

/* One oversampled Vbat reading in mV (applies the x2.0 divider). Returns 0 on
 * error or if never initialized. Safe to call from any task. */
int battery_read_mv(void);

/* Coarse single-LiPo state-of-charge curve: 0% @ VBAT_MIN_MV, 100% @
 * VBAT_FULL_MV, clamped. Not a fuel gauge — good enough for a status display
 * and a low-battery gate. */
uint8_t battery_percent(int vbat_mv);

/* Start the low-priority ~30 s sampling task (caches the latest reading).
 * Idempotent: safe to call once from app_main after battery_init. */
void battery_start_monitor(void);

/* Last sampled Vbat mV (0 if never sampled). Read by the Phase 4 status
 * characteristic. Cheap atomic read of a 32-bit aligned value. */
int battery_latest_mv(void);

#ifdef __cplusplus
}
#endif