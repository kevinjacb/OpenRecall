/*
 * Button test — momentary switch on D2 (GPIO3, strapping pin).
 *
 * Wired button->GND with the internal pull-up, so press = LOW. Polls with a
 * ~20 ms debounce and waits up to BUTTON_TIMEOUT_MS for a press. GPIO3 is a
 * strapping pin but only matters at reset (a held-down button at boot would
 * change JTAG routing); reading it at runtime is fine.
 */
#include "selftest.h"
#include "selftest_config.h"
#include "esp_log.h"
#include "esp_err.h"
#include "driver/gpio.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdio.h>

static const char *TAG = "button";

selftest_result_t test_button(void) {
  selftest_result_t r = {0};
  gpio_config_t io = {
    .pin_bit_mask = 1ULL << BUTTON_GPIO, .mode = GPIO_MODE_INPUT,
    .pull_up_en = GPIO_PULLUP_ENABLE, .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE };
  esp_err_t err = gpio_config(&io);
  if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "cfg: %s", esp_err_to_name(err)); return r; }

  printf(">>> Press the D2 button now (waiting up to %d s)...\n", BUTTON_TIMEOUT_MS / 1000);
  ESP_LOGI(TAG, "waiting for press on GPIO%d (D2)...", BUTTON_GPIO);

  int pressed = 0;
  TickType_t deadline = xTaskGetTickCount() + pdMS_TO_TICKS(BUTTON_TIMEOUT_MS);
  while (xTaskGetTickCount() < deadline) {
    if (gpio_get_level(BUTTON_GPIO) == 0) {        /* press = LOW */
      vTaskDelay(pdMS_TO_TICKS(BUTTON_DEBOUNCE_MS));
      if (gpio_get_level(BUTTON_GPIO) == 0) {
        pressed++;
        while (gpio_get_level(BUTTON_GPIO) == 0) vTaskDelay(pdMS_TO_TICKS(10));  /* wait for release */
        break;
      }
    }
    vTaskDelay(pdMS_TO_TICKS(10));
  }
  if (pressed > 0) {
    r.status = ST_PASS;
    snprintf(r.detail, sizeof r.detail, "%d press(es) detected", pressed);
  } else {
    r.status = ST_FAIL;
    snprintf(r.detail, sizeof r.detail, "no press within %ds", BUTTON_TIMEOUT_MS / 1000);
  }
  return r;
}