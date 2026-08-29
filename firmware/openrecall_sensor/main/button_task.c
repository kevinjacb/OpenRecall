/*
 * D1 button firmware wiring (ESP-only). See button_task.h.
 */
#include "button_task.h"
#include "button.h"
#include "config.h"
#include "executor.h"
#include "audio_gate.h"
#include "battery.h"

#include "driver/gpio.h"
#include "esp_log.h"
#include "esp_timer.h"
#include "esp_sleep.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "button";

static uint32_t now_ms(void) {
  /* esp_timer_get_time() is monotonic us since boot; ms is enough resolution
   * for the gesture thresholds (>= 400 ms). Wraps at ~49 days — acceptable. */
  return (uint32_t)(esp_timer_get_time() / 1000);
}

static void dispatch_gesture(button_gesture_t g) {
  cmd_request_t req;
  switch (g) {
    case BTN_SHORT: {
      req = (cmd_request_t){ .type = CMD_REQUEST_BUFFER, .status = EXEC_OK, .seconds = 60 };
      ESP_LOGI(TAG, "SHORT -> request_buffer 60s (mark moment)");
      executor_dispatch_local(&req);
      break;
    }
    case BTN_DOUBLE: {
      /* Toggle: if capturing (not paused) -> stop; if stopped (paused) -> start. */
      bool was_paused = audio_gate_paused();
      if (was_paused) {
        req = (cmd_request_t){ .type = CMD_START_AUDIO, .status = EXEC_OK };
        ESP_LOGI(TAG, "DOUBLE -> start_audio (was stopped)");
      } else {
        req = (cmd_request_t){ .type = CMD_STOP_AUDIO, .status = EXEC_OK };
        ESP_LOGI(TAG, "DOUBLE -> stop_audio (was capturing)");
      }
      executor_dispatch_local(&req);
      break;
    }
    case BTN_LONG: {
      req = (cmd_request_t){ .type = CMD_CAPTURE_PHOTO, .status = EXEC_OK };
      ESP_LOGI(TAG, "LONG -> capture_photo (snapshot now)");
      executor_dispatch_local(&req);
      break;
    }
    case BTN_VERY_LONG: {
      /* Explicit deep sleep — drops the PSRAM ring (fresh session on wake).
       * Wait a beat so the user can release the button before we arm the wake
       * (otherwise a still-held LOW would re-wake immediately). GPIO2 is
       * RTC_GPIO2 on the S3, so ext0 (RTC GPIO) deep-sleep wake works. Wake on
       * the next press (active-low -> level 0). ext0 keeps the RTC peri on, so
       * sleep current is higher than light sleep — acceptable for an explicit
       * user-initiated sleep. ON-HW VALIDATION: confirm wake + reset behavior
       * (Task 8). */
      ESP_LOGI(TAG, "VERY_LONG -> deep sleep (explicit; wakes on next press)");
      /* Tell the phone/server we are going down cleanly (state:"sleeping")
       * rather than just dropping the link; the 500 ms release-wait below
       * doubles as time for the notification to flush. */
      battery_notify_sleeping();
      vTaskDelay(pdMS_TO_TICKS(500));
      esp_sleep_enable_ext0_wakeup(BUTTON_GPIO, 0);   /* wake when GPIO2 goes LOW */
      esp_deep_sleep_start();
      /* does not return; on wake the device reboots (fresh session) */
      break;
    }
    default:
      break;  /* BTN_NONE */
  }
}

static void button_task(void *arg) {
  (void)arg;
  /* GPIO2: input + internal pull-up; press pulls it LOW. We poll (no ISR) at
   * the debounce cadence — a 20 ms poll IS the debounce for a mechanical
   * button whose bounce is sub-20 ms, and the classifier's thresholds are all
   * >= 400 ms so residual jitter cannot create a false gesture. */
  gpio_config_t io = {
    .pin_bit_mask = (1ULL << BUTTON_GPIO),
    .mode = GPIO_MODE_INPUT,
    .pull_up_en = GPIO_PULLUP_ENABLE,
    .pull_down_en = GPIO_PULLDOWN_DISABLE,
    .intr_type = GPIO_INTR_DISABLE,
  };
  if (gpio_config(&io) != ESP_OK) {
    ESP_LOGE(TAG, "gpio_config failed — button disabled");
    vTaskDelete(NULL);
    return;
  }

  button_classifier_t cls;
  button_classifier_init(&cls, now_ms());

  /* Poll the GPIO every BUTTON_DEBOUNCE_MS and feed the classifier the current
   * level. The poll cadence IS the debounce for the XIAO tactile (bounce
   * settles well under 20 ms); the classifier's thresholds are all >= 400 ms so
   * residual jitter cannot manufacture a gesture. The classifier must be fed
   * every tick (not only on transitions) so it can expire the single-tap gap
   * window -> SHORT and fire very-long in-band. For a noisier button, gate the
   * `level` commit on N stable reads here. */
  int level = gpio_get_level(BUTTON_GPIO);   /* 1 = released (HIGH), 0 = pressed */
  for (;;) {
    level = gpio_get_level(BUTTON_GPIO);
    button_gesture_t g = button_classifier_event(&cls, /*pressed=*/(level == 0), now_ms());
    if (g != BTN_NONE) dispatch_gesture(g);
    vTaskDelay(pdMS_TO_TICKS(BUTTON_DEBOUNCE_MS));
  }
}

esp_err_t button_start(void) {
  BaseType_t ok = xTaskCreatePinnedToCore(button_task, "button", 4096, NULL, 4, NULL, 0);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "button task create failed");
    return ESP_FAIL;
  }
  ESP_LOGI(TAG, "button gesture task up (GPIO%d, poll %d ms)", BUTTON_GPIO, BUTTON_DEBOUNCE_MS);
  return ESP_OK;
}