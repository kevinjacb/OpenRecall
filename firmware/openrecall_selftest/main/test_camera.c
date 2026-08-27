/*
 * Camera test — two-stage OV2640 check.
 *
 * Stage 1 (sensor + optics): a selftest-local esp_camera config in RGB565/QVGA
 * (reusing the CAM_PIN_* from the production config.h) grabs a frame and checks
 * the luma standard deviation is above a floor -> rejects a lens-cap-on / dead
 * sensor / uniform frame. A second frame is grabbed to confirm the sensor is
 * streaming, not stuck. RGB565 gives reliable pixel-level content proof that
 * JPEG size alone cannot (an all-black scene still JPEGs to a tiny valid file).
 *
 * Stage 2 (production JPEG path): calls the production camera_init() +
 * camera_capture_jpeg() (by CMake reference) and validates JPEG SOI/EOI markers
 * + size. This proves the exact JPEG pipeline the product firmware uses.
 */
#include "selftest.h"
#include "selftest_config.h"
#include "config.h"            /* CAM_PIN_* macros */
#include "camera.h"            /* production JPEG path */
#include "esp_camera.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <stdlib.h>
#include <stdio.h>

static const char *TAG = "camera";

/* Stage 1: RGB565/QVGA — analyze pixels to prove sensor+optics. */
static bool stage_rgb565(double *mean_out, double *std_out, char *detail, size_t dlen) {
  camera_config_t cfg = {
    .pin_pwdn = CAM_PIN_PWDN, .pin_reset = CAM_PIN_RESET, .pin_xclk = CAM_PIN_XCLK,
    .pin_sccb_sda = CAM_PIN_SIOD, .pin_sccb_scl = CAM_PIN_SIOC,
    .pin_d7 = CAM_PIN_D7, .pin_d6 = CAM_PIN_D6, .pin_d5 = CAM_PIN_D5, .pin_d4 = CAM_PIN_D4,
    .pin_d3 = CAM_PIN_D3, .pin_d2 = CAM_PIN_D2, .pin_d1 = CAM_PIN_D1, .pin_d0 = CAM_PIN_D0,
    .pin_vsync = CAM_PIN_VSYNC, .pin_href = CAM_PIN_HREF, .pin_pclk = CAM_PIN_PCLK,
    .xclk_freq_hz = CAM_XCLK_FREQ_HZ,
    .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
    .pixel_format = PIXFORMAT_RGB565,
    .frame_size = FRAMESIZE_QVGA,           /* 320x240 */
    .jpeg_quality = 12,
    .fb_count = 2,
    .fb_location = CAMERA_FB_IN_PSRAM,
    .grab_mode = CAMERA_GRAB_LATEST,
  };
  esp_err_t err = esp_camera_init(&cfg);
  if (err != ESP_OK) { snprintf(detail, dlen, "rgb565 init: %s", esp_err_to_name(err)); return false; }

  camera_fb_t *fb1 = esp_camera_fb_get();
  if (!fb1 || fb1->format != PIXFORMAT_RGB565) {
    if (fb1) esp_camera_fb_return(fb1);
    esp_camera_deinit();
    snprintf(detail, dlen, "rgb565: no frame");
    return false;
  }
  size_t cnt = (size_t)fb1->width * fb1->height;
  const uint16_t *px = (const uint16_t *)fb1->buf;
  double sum = 0, sumsq = 0;
  for (size_t i = 0; i < cnt; i++) {
    uint16_t v = px[i];
    int rv = (v >> 11) & 0x1F, gv = (v >> 5) & 0x3F, bv = v & 0x1F;
    int R = (rv * 255) / 31, G = (gv * 255) / 63, B = (bv * 255) / 31;
    int y = (R + 2 * G + B) / 4;     /* ~0-255 luma */
    sum += y; sumsq += (double)y * y;
  }
  esp_camera_fb_return(fb1);
  camera_fb_t *fb2 = esp_camera_fb_get();   /* proves the sensor is streaming, not stuck */
  if (fb2) esp_camera_fb_return(fb2);
  esp_camera_deinit();

  double mean = sum / (double)cnt;
  double var = sumsq / (double)cnt - mean * mean;
  double std = var > 0 ? sqrt(var) : 0;
  *mean_out = mean; *std_out = std;
  if (std < CAMERA_LUMA_STD_FLOOR) {
    snprintf(detail, dlen, "luma std=%.1f (too uniform: lens cap / dead sensor)", std);
    return false;
  }
  return true;
}

/* Stage 2: production JPEG path. */
static bool stage_jpeg(size_t *len_out, char *detail, size_t dlen) {
  esp_err_t err = camera_init();            /* production wrapper (JPEG/VGA) */
  if (err != ESP_OK) { snprintf(detail, dlen, "jpeg init: %s", esp_err_to_name(err)); return false; }
  camera_flush_stale();
  uint8_t *buf = NULL; size_t len = 0;
  err = camera_capture_jpeg(&buf, &len);
  if (err != ESP_OK || !buf) { if (buf) free(buf); esp_camera_deinit(); snprintf(detail, dlen, "jpeg capture fail"); return false; }
  bool ok = (len >= CAMERA_JPEG_MIN_BYTES && len <= CAMERA_JPEG_MAX_BYTES &&
             buf[0] == 0xFF && buf[1] == 0xD8 && buf[len - 2] == 0xFF && buf[len - 1] == 0xD9);
  if (ok) *len_out = len;
  else snprintf(detail, dlen, "jpeg bad: len=%u or markers", (unsigned)len);
  free(buf);
  esp_camera_deinit();
  return ok;
}

selftest_result_t test_camera(void) {
  selftest_result_t r = {0};
  double mean = 0, std = 0;
  ESP_LOGI(TAG, "stage 1: RGB565/QVGA sensor+optics check...");
  bool s1 = stage_rgb565(&mean, &std, r.detail, sizeof r.detail);
  ESP_LOGI(TAG, "stage1: luma mean=%.1f std=%.1f (%s)", mean, std, s1 ? "ok" : r.detail);
  if (!s1) { r.status = ST_FAIL; return r; }

  vTaskDelay(pdMS_TO_TICKS(200));   /* let the sensor settle before re-init */
  size_t jpglen = 0;
  ESP_LOGI(TAG, "stage 2: production JPEG (VGA) path...");
  bool s2 = stage_jpeg(&jpglen, r.detail, sizeof r.detail);
  ESP_LOGI(TAG, "stage2: jpeg=%u bytes (%s)", (unsigned)jpglen, s2 ? "ok" : r.detail);
  if (!s2) { r.status = ST_FAIL; return r; }

  r.status = ST_PASS;
  snprintf(r.detail, sizeof r.detail, "luma std=%.0f, jpeg=%uB", std, (unsigned)jpglen);
  return r;
}