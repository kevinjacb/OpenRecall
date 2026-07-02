/*
 * Opus encoder implementation.
 *
 * REQUIRES a libopus component. On the bench, add one of:
 *   - a managed component:  idf.py add-dependency "<namespace>/libopus"
 *   - or vendor esp-adf's / pschatzmann's libopus into components/
 * Then add "opus_stream.c" to main/CMakeLists.txt SRCS and "<the opus component>"
 * to its REQUIRES, and call opus_stream_init()/_encode() from the audio task.
 *
 * Until then this file is intentionally excluded from the build so the firmware
 * stays green; the code below is the real, standard-libopus implementation.
 */
#include "opus_stream.h"

#include "esp_check.h"
#include "opus.h"

static const char *TAG = "opus";

static OpusEncoder *s_enc;

esp_err_t opus_stream_init(void) {
  int err = 0;
  s_enc = opus_encoder_create(SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_VOIP, &err);
  if (err != OPUS_OK || s_enc == NULL) {
    ESP_LOGE(TAG, "opus_encoder_create failed: %s", opus_strerror(err));
    return ESP_FAIL;
  }
  opus_encoder_ctl(s_enc, OPUS_SET_BITRATE(OPUS_BITRATE));
  opus_encoder_ctl(s_enc, OPUS_SET_COMPLEXITY(OPUS_COMPLEXITY));
  opus_encoder_ctl(s_enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
  ESP_LOGI(TAG, "Opus up: %d bps, complexity %d", OPUS_BITRATE, OPUS_COMPLEXITY);
  return ESP_OK;
}

int opus_stream_encode(const int16_t *pcm, uint8_t *out, int cap) {
  int n = opus_encode(s_enc, pcm, FRAME_SAMPLES, out, cap);
  if (n < 0) {
    ESP_LOGE(TAG, "opus_encode failed: %s", opus_strerror(n));
    return -1;
  }
  return n;
}
