/*
 * OpenRecall self-test firmware — entry point.
 *
 * (Scaffold build: prints a banner and lights the LED. The full test
 * sequence, summary table, LED blink, and live mic meter are added once the
 * component test modules land.)
 */
#include "selftest.h"
#include "selftest_config.h"
#include "esp_log.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdio.h>

static const char *TAG = "selftest";

void selftest_led_init(void) {
  gpio_config_t io = {
    .pin_bit_mask = 1ULL << LED_GPIO, .mode = GPIO_MODE_OUTPUT,
    .pull_up_en = GPIO_PULLUP_DISABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE };
  gpio_config(&io);
  gpio_set_level(LED_GPIO, LED_OFF);
}
void selftest_led_set_all_pass(void) { gpio_set_level(LED_GPIO, LED_ON); }
void selftest_led_start_fail_blink(int n) { (void)n; gpio_set_level(LED_GPIO, LED_OFF); }

void app_main(void) {
  selftest_led_init();
  printf("\n\n====== OpenRecall Hardware Self-Test ======\n");
  printf("Board: XIAO ESP32S3 Sense | IDF 5.1.6\n");
  printf("audio: mics D3-5/GPIO4-6 | camera: OV2640 | button: D2/GPIO3 | battery: D1/GPIO2\n");
  printf("LED: GPIO21 - solid ON=all pass; blink N=N failures\n\n");
  selftest_led_set_all_pass();
  ESP_LOGI(TAG, "scaffold build OK");
  while (1) vTaskDelay(pdMS_TO_TICKS(1000));
}