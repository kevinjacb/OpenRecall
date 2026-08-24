#include "camera.h"
#include "config.h"
#include "esp_camera.h"
#include "esp_log.h"
#include <stdlib.h>
#include <string.h>

static const char *TAG = "camera";

esp_err_t camera_init(void) {
  camera_config_t cfg = {
    .pin_pwdn     = CAM_PIN_PWDN,
    .pin_reset    = CAM_PIN_RESET,
    .pin_xclk     = CAM_PIN_XCLK,
    .pin_sccb_sda = CAM_PIN_SIOD,
    .pin_sccb_scl = CAM_PIN_SIOC,
    .pin_d7 = CAM_PIN_D7, .pin_d6 = CAM_PIN_D6, .pin_d5 = CAM_PIN_D5, .pin_d4 = CAM_PIN_D4,
    .pin_d3 = CAM_PIN_D3, .pin_d2 = CAM_PIN_D2, .pin_d1 = CAM_PIN_D1, .pin_d0 = CAM_PIN_D0,
    .pin_vsync = CAM_PIN_VSYNC, .pin_href = CAM_PIN_HREF, .pin_pclk = CAM_PIN_PCLK,
    .xclk_freq_hz = CAM_XCLK_FREQ_HZ,
    .ledc_timer = LEDC_TIMER_0, .ledc_channel = LEDC_CHANNEL_0,
    .pixel_format = PIXFORMAT_JPEG,
    .frame_size   = FRAMESIZE_VGA,     /* 640x480 */
    .jpeg_quality = VIDEO_JPEG_QUALITY,
    .fb_count     = 2,                 /* double-buffer for streaming */
    .fb_location  = CAMERA_FB_IN_PSRAM,
    .grab_mode    = CAMERA_GRAB_LATEST,
  };
  esp_err_t err = esp_camera_init(&cfg);
  if (err != ESP_OK) ESP_LOGE(TAG, "esp_camera_init: %s", esp_err_to_name(err));
  return err;
}

esp_err_t camera_capture_jpeg(uint8_t **out_buf, size_t *out_len) {
  camera_fb_t *fb = esp_camera_fb_get();
  if (!fb || fb->format != PIXFORMAT_JPEG) {
    if (fb) esp_camera_fb_return(fb);
    return ESP_FAIL;
  }
  uint8_t *copy = malloc(fb->len);
  if (!copy) { esp_camera_fb_return(fb); return ESP_FAIL; }
  memcpy(copy, fb->buf, fb->len);
  *out_buf = copy; *out_len = fb->len;
  esp_camera_fb_return(fb);
  return ESP_OK;
}