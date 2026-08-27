/*
 * OpenRecall self-test firmware — entry point + orchestration.
 *
 * Runs four component tests sequentially (audio -> camera -> button -> battery),
 * prints a summary table, sets the LED state (solid ON = all pass; blink N =
 * N failures), then enters a live mic meter loop (per-channel RMS every 500 ms)
 * until the D2 button is pressed or the device is reset.
 *
 * Reuses the production audio_capture.c (I2S mics) and camera.c (OV2640) by
 * CMake reference; the audio path stays initialized after test_audio so the
 * live meter can read it.
 */
#include "selftest.h"
#include "selftest_config.h"
#include "audio_capture.h"
#include "config.h"          /* FRAME_SAMPLES */
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <math.h>

static const char *status_str(selftest_status_t s) {
  return s == ST_PASS ? "PASS" : s == ST_FAIL ? "FAIL" : "SKIP";
}

static void blink_task(void *arg) {
  int n = (int)(intptr_t)arg;
  while (1) {
    for (int i = 0; i < n; i++) {
      gpio_set_level(LED_GPIO, LED_ON);
      vTaskDelay(pdMS_TO_TICKS(200));
      gpio_set_level(LED_GPIO, LED_OFF);
      vTaskDelay(pdMS_TO_TICKS(200));
    }
    vTaskDelay(pdMS_TO_TICKS(1500));
  }
}

void selftest_led_init(void) {
  gpio_config_t io = {
    .pin_bit_mask = 1ULL << LED_GPIO, .mode = GPIO_MODE_OUTPUT,
    .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE };
  gpio_config(&io);
  gpio_set_level(LED_GPIO, LED_OFF);
}
void selftest_led_set_all_pass(void) { gpio_set_level(LED_GPIO, LED_ON); }
void selftest_led_start_fail_blink(int n) {
  xTaskCreate(blink_task, "led", 2048, (void *)(intptr_t)n, 1, NULL);
}

static void run_one(const char *name, selftest_result_t *out) {
  printf("[%s] running...\n", name);
  if      (strcmp(name, "audio")  == 0) *out = test_audio();
  else if (strcmp(name, "camera") == 0) *out = test_camera();
  else if (strcmp(name, "button") == 0) *out = test_button();
  else                                   *out = test_battery();
  printf("[%s] %s  %s\n", name, status_str(out->status), out->detail);
}

void app_main(void) {
  selftest_led_init();
  printf("\n\n====== OpenRecall Hardware Self-Test ======\n");
  printf("Board: XIAO ESP32S3 Sense | IDF 5.1.6\n");
  printf("audio: mics D3-5/GPIO4-6 | camera: OV2640 | button: D2/GPIO3 | battery: D1/GPIO2\n");
  printf("LED: GPIO21 - solid ON = all pass; blink N = N failures\n\n");

  selftest_result_t res[4];
  const char *names[4] = { "audio", "camera", "button", "battery" };
  for (int i = 0; i < 4; i++) run_one(names[i], &res[i]);

  int fails = 0;
  printf("\n====== SUMMARY ======\n");
  for (int i = 0; i < 4; i++) {
    printf("  %-7s %-4s %s\n", names[i], status_str(res[i].status), res[i].detail);
    if (res[i].status != ST_PASS) fails++;
  }
  printf("=====================\n");
  if (fails == 0) { printf("ALL PASS - safe to seal the device.\n"); selftest_led_set_all_pass(); }
  else            { printf("%d FAIL(s) - do NOT seal.\n", fails); selftest_led_start_fail_blink(fails); }

  printf("\n[live mic meter] per-channel RMS every %d ms. Press D2 to exit.\n",
         LIVE_METER_FRAMES * 20);
  static int16_t p[FRAME_SAMPLES], rr[FRAME_SAMPLES];   /* static: keep off the main stack */
  while (gpio_get_level(BUTTON_GPIO) != 0) {
    long sq_p = 0, sq_r = 0; long n = 0;
    for (int f = 0; f < LIVE_METER_FRAMES && gpio_get_level(BUTTON_GPIO) != 0; f++) {
      if (audio_capture_read_stereo(p, rr) != ESP_OK) { sq_p = sq_r = 0; break; }
      for (int i = 0; i < FRAME_SAMPLES; i++) { sq_p += (long)p[i] * p[i]; sq_r += (long)rr[i] * rr[i]; n++; }
    }
    if (n > 0) printf("live: pri_rms=%.0f ref_rms=%.0f\n", sqrt((double)sq_p / n), sqrt((double)sq_r / n));
  }
  printf("Button pressed - exiting live meter. Reset to re-run.\n");
  while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}