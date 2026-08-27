/*
 * OpenRecall self-test firmware — hardware constants.
 *
 * Pins are XIAO ESP32S3 Sense D-label -> GPIO (Seeed wiki) cross-checked
 * against IDF soc/esp32s3/include/soc/adc_channel.h. Thresholds are tuned
 * for the INMP441 dual-mic array (see ../openrecall_sensor/main/config.h notes).
 *
 * Pure C / host-compilable (<stdint.h> only).
 */
#pragma once
#include <stdint.h>

/* ---- Pins ----
 * Audio mics reuse the production drivers (I2S on GPIO 4/5/6 = D3/D4/D5);
 * the camera reuses the production driver (OV2640 on the Sense board).
 * Only the button + battery are new here. */
#define VBAT_GPIO         2     /* D1, ADC1 channel 1 (battery divider) */
#define BUTTON_GPIO       3     /* D2, strapping pin; internal pull-up, press = LOW */
#define LED_GPIO         21      /* USER_LED on the Sense (also SD CS; SD is skipped) */

/* XIAO USER_LED is active-low (drive 0 = ON). Flip both if your board differs. */
#define LED_ON            0
#define LED_OFF           1

/* ---- Battery divider (100k/100k, x2.0) ---- */
#define VBAT_DIVIDER      2.0f
#define VBAT_MIN_MV     3000     /* 3.0V single-LiPo floor */
#define VBAT_MAX_MV     4250     /* 4.25V; above 4.2V full-charge is an error */

/* ---- Button test ---- */
#define BUTTON_TIMEOUT_MS  10000
#define BUTTON_DEBOUNCE_MS 20

/* ---- Audio test (interactive 3 s) ----
 * INMP441 noise floor RMS ~60-70; speech/blow ~300-450 (per config.h notes).
 * 200 sits above idle noise, below real stimulus -> requires audible input.
 * STEREO floor: (primary-reference) RMS proves the two channels are distinct
 * (not a collapsed/locked bus or shorted L/R). Independent noise alone gives
 * ~1.4x noise here, so 20 is a safe low floor. */
#define AUDIO_TEST_FRAMES   150   /* 3 s @ 20 ms/frame */
#define AUDIO_RMS_FLOOR     200
#define AUDIO_STEREO_FLOOR  20

/* ---- Camera test ---- */
#define CAMERA_LUMA_STD_FLOOR  20     /* 8-bit luma std-dev; rejects uniform/black */
#define CAMERA_JPEG_MIN_BYTES  2000
#define CAMERA_JPEG_MAX_BYTES  200000

/* ---- Live mic meter (post-summary loop) ---- */
#define LIVE_METER_FRAMES      25     /* 500 ms @ 20 ms/frame */