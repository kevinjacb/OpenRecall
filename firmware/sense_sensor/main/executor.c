#include "executor.h"

#include "audio_gate.h"
#include "ble_drain.h"
#include "config.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/queue.h"
#include "freertos/task.h"

static const char *TAG = "exec";

static QueueHandle_t s_queue;

static void executor_task(void *arg) {
  (void)arg;
  static cmd_request_t req;
  for (;;) {
    if (xQueueReceive(s_queue, &req, portMAX_DELAY) != pdPASS) continue;
    if (req.status != EXEC_OK) {
      ESP_LOGW(TAG, "drop status=%d type=%d (no work)", req.status, req.type);
      continue;
    }
    switch (req.type) {
      case CMD_START_AUDIO:
        audio_gate_set(false);
        ESP_LOGI(TAG, "start_audio");
        break;
      case CMD_STOP_AUDIO:
        audio_gate_set(true);
        ESP_LOGI(TAG, "stop_audio");
        break;
      case CMD_REQUEST_BUFFER: {
        QueueHandle_t rq = ble_drain_replay_queue();
        if (rq == NULL) {
          ESP_LOGW(TAG, "request_buffer: drain replay queue not ready — dropped");
          break;
        }
        replay_request_t rr = { .seconds = req.seconds };
        if (xQueueSend(rq, &rr, 0) != pdPASS) {
          ESP_LOGW(TAG, "replay queue full — dropped seconds=%u", (unsigned)req.seconds);
        }
        break;
      }
      case CMD_CAPTURE_PHOTO:
        ESP_LOGI(TAG, "capture_photo not implemented (P4b)");
        break;
      case CMD_RECORD_VIDEO:
        ESP_LOGI(TAG, "record_video not implemented (P4b) dur=%u", (unsigned)req.duration_s);
        break;
      default:
        ESP_LOGW(TAG, "unknown cmd type=%d", req.type);
        break;
    }
  }
}

esp_err_t executor_init(void) {
  s_queue = xQueueCreate(EXECUTOR_QUEUE_DEPTH, sizeof(cmd_request_t));
  if (s_queue == NULL) {
    ESP_LOGE(TAG, "executor queue create failed");
    return ESP_FAIL;
  }
  BaseType_t ok = xTaskCreatePinnedToCore(executor_task, "executor",
                                          EXECUTOR_TASK_STACK, NULL,
                                          EXECUTOR_TASK_PRIO, NULL, EXECUTOR_TASK_CORE);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "executor task create failed");
    vQueueDelete(s_queue);
    s_queue = NULL;
    return ESP_FAIL;
  }
  ESP_LOGI(TAG, "executor up: queue=%d task_stack=%d core=%d",
           EXECUTOR_QUEUE_DEPTH, EXECUTOR_TASK_STACK, EXECUTOR_TASK_CORE);
  return ESP_OK;
}

bool executor_submit(const char *type, const cJSON *params) {
  static cmd_request_t req;
  executor_parse_and_validate(type, params, &req);
  if (xQueueSend(s_queue, &req, 0) != pdPASS) {
    ESP_LOGW(TAG, "executor queue full — dropped type=%s", type);
    return false;
  }
  return true;
}