/*
 * Audio test — interactive 3 s stereo capture from the dual IENMP441 array.
 *
 * Reuses the production audio_capture.c (by CMake reference) so the rig
 * exercises the REAL I2S driver. Prompts the user to speak/blow, captures 150
 * frames (3 s), and computes per-channel RMS + peak and the RMS of
 * (primary - reference). PASS requires both channels above the noise floor
 * (audible stimulus) AND the difference RMS above a small floor (proves the
 * two channels are distinct -> not a collapsed/locked bus or shorted L/R).
 */
#include "selftest.h"
#include "selftest_config.h"
#include "audio_capture.h"
#include "config.h"          /* FRAME_SAMPLES */
#include "esp_log.h"
#include "esp_err.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <math.h>
#include <stdlib.h>
#include <stdio.h>

static const char *TAG = "audio";

selftest_result_t test_audio(void) {
  selftest_result_t r = {0};
  esp_err_t err = audio_capture_init();
  if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "I2S init: %s", esp_err_to_name(err)); return r; }

  printf(">>> Please SPEAK or BLOW into BOTH mics for 3 seconds...\n");
  vTaskDelay(pdMS_TO_TICKS(700));     /* let the user read the prompt */

  static int16_t prim[FRAME_SAMPLES];
  static int16_t ref[FRAME_SAMPLES];
  long sq_p = 0, sq_r = 0, sq_d = 0;
  int peak_p = 0, peak_r = 0;
  long n = 0;
  for (int f = 0; f < AUDIO_TEST_FRAMES; f++) {
    err = audio_capture_read_stereo(prim, ref);
    if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "read err: %s", esp_err_to_name(err)); return r; }
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      int p = prim[i], rr = ref[i];
      sq_p += (long)p * p;
      sq_r += (long)rr * rr;
      int d = p - rr;
      sq_d += (long)d * d;
      int ap = p < 0 ? -p : p; if (ap > peak_p) peak_p = ap;
      int ar = rr < 0 ? -rr : rr; if (ar > peak_r) peak_r = ar;
      n++;
    }
    if (f % 50 == 0) printf("  audio %d/%d frames\n", f, AUDIO_TEST_FRAMES);
  }
  double rms_p = sqrt((double)sq_p / n);
  double rms_r = sqrt((double)sq_r / n);
  double rms_d = sqrt((double)sq_d / n);
  ESP_LOGI(TAG, "rms_pri=%.1f rms_ref=%.1f peak_pri=%d peak_ref=%d rms_diff=%.1f",
           rms_p, rms_r, peak_p, peak_r, rms_d);

  if (rms_p > AUDIO_RMS_FLOOR && rms_r > AUDIO_RMS_FLOOR && rms_d > AUDIO_STEREO_FLOOR) {
    r.status = ST_PASS;
    snprintf(r.detail, sizeof r.detail, "pri rms=%.0f ref rms=%.0f diff=%.0f", rms_p, rms_r, rms_d);
  } else {
    r.status = ST_FAIL;
    snprintf(r.detail, sizeof r.detail, "pri=%.0f ref=%.0f diff=%.0f (floor %d/%d/%d)",
             rms_p, rms_r, rms_d, AUDIO_RMS_FLOOR, AUDIO_RMS_FLOOR, AUDIO_STEREO_FLOOR);
  }
  return r;
}