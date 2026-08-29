/*
 * P4b ambient snapshot — see snapshot.h. The timer callback runs on the
 * FreeRTOS timer service task but does NO heavy work: it only xTaskNotifyGive
 * the snapshot worker. The capture (camera_capture_jpeg + SD write + manifest)
 * runs on the 8 KB worker task, not the 2 KB Tmr Svc stack — running it on the
 * timer task overflowed Tmr Svc and panicked. The SD write (~92 ms worst case,
 * Spike-2) happens on the worker, not the audio real-time path, so it cannot
 * stall §C.6. rel_ts_ms_now() reads a volatile uint32 updated by the audio task
 * each frame — a 32-bit aligned read is atomic on the ESP32, sufficient for a
 * ms clock shared between cores.
 */
#include "snapshot.h"
#include "boot_id.h"
#include "camera.h"
#include "config.h"
#include "media_index.h"
#include "sd_store.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/timers.h"

#include <errno.h>
#include <inttypes.h>  /* PRIu32 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static const char *TAG = "snapshot";

/* rel_ts_ms_now() + the s_rel_ts_ms it reads live in openrecall_sensor.c (the
 * audio task owns the clock and updates it each frame). See snapshot.h for the
 * declaration. */

static TimerHandle_t s_timer = NULL;
static TaskHandle_t  s_worker = NULL;  /* capture runs here, NOT on the timer task */
static uint32_t s_seq = 0;   /* monotonic per-boot counter */

/* The worker task: blocks on a task notification, then runs one capture on the
 * worker's 8 KB stack. The timer callback and snapshot_capture_async() only
 * xTaskNotifyGive (instant, minimal stack) — the heavy camera_capture_jpeg +
 * SD write + manifest append all happen here. 8 KB matches the video task,
 * which does the same camera_capture_jpeg + SD work per frame; the executor
 * task's 4 KB stack was sized for cJSON parse + queue send only and the 2 KB
 * Tmr Svc stack cannot hold a camera capture at all. Core 0, priority 4 —
 * below the audio encode task (the hard real-time constraint), same as the
 * video task. Burst coalescing: ulTaskNotifyTake(pdTRUE) clears the count on
 * wake, so several close notifications fire one capture — fine for ambient
 * samples. */
static void snapshot_worker_task(void *arg) {
  (void)arg;
  for (;;) {
    (void)ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
    (void)snapshot_capture_one(rel_ts_ms_now());
  }
}

void snapshot_capture_async(void) {
  /* Signal the worker to capture one frame now. Safe from the timer service
   * task, the executor task, or any other task — xTaskNotifyGive is non-
   * blocking and ISR/timer-safe. No-op if the worker isn't up yet. */
  if (s_worker) {
    xTaskNotifyGive(s_worker);
  }
}

/* One-shot mkdir that treats "already exists" as success. The per-boot
 * snapshot subdir (/sdcard/snapshots/<boot_id>) is not created by sd_store_mount
 * (only the top-level /sdcard/snapshots is), so ensure it before the write. */
static esp_err_t ensure_dir(const char *path) {
  if (mkdir(path, 0777) == 0) return ESP_OK;
  if (errno == EEXIST) return ESP_OK;
  ESP_LOGE(TAG, "mkdir(%s): %s", path, strerror(errno));
  return ESP_FAIL;
}

esp_err_t snapshot_capture_one(uint32_t rel_ts_ms) {
  esp_err_t err = sd_store_mount();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "sd_store_mount: %s", esp_err_to_name(err));
    return err;
  }

  /* fb_count=1 + CAMERA_GRAB_WHEN_EMPTY leaves the last captured frame in the
   * buffer while idle — up to SNAPSHOT_INTERVAL_S old. Drain it so this
   * snapshot reflects ~now and its rel_ts matches the image, then capture a
   * fresh frame. The flush is instant when the buffer is full (the normal idle
   * case); camera_capture_jpeg then blocks ~40 ms for the fresh frame. Only the
   * ambient path does this — the video loop keeps the pipeline fresh. */
  camera_flush_stale();

  uint8_t *jpg = NULL;
  size_t n = 0;
  err = camera_capture_jpeg(&jpg, &n);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "camera_capture_jpeg: %s", esp_err_to_name(err));
    return err;
  }

  uint32_t boot = boot_id_get();
  uint32_t seq = s_seq++;
  char path[NAME_MAX_LEN];
  snapshot_path(path, sizeof path, boot, rel_ts_ms, seq);

  /* Ensure the per-boot subdir exists before the write. */
  char dir[NAME_MAX_LEN];
  snprintf(dir, sizeof dir, "/sdcard/snapshots/%" PRIu32, (uint32_t)boot);
  (void)ensure_dir(dir);

  err = sd_store_write(path, jpg, n);
  if (err != ESP_OK) {
    /* Data-integrity: do NOT append a manifest line if the write failed — it
     * would point at a partial/missing file. Log, free, and return. */
    ESP_LOGE(TAG, "sd_store_write(%s): %s — manifest NOT updated", path,
             esp_err_to_name(err));
    free(jpg);
    return err;
  }

  char line[NAME_MAX_LEN + 32];
  manifest_line(line, sizeof line, path, rel_ts_ms, boot, 's');
  err = sd_store_append(SD_MANIFEST, line);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "sd_store_append(manifest): %s", esp_err_to_name(err));
    /* The JPEG is on the card but the manifest is missing this entry; the
     * server won't list it until a later capture re-appends. Not fatal — the
     * file exists. */
  }

  ESP_LOGI(TAG, "snap %s (%zu bytes) ts=%" PRIu32, path, n,
           (uint32_t)rel_ts_ms);
  free(jpg);
  return ESP_OK;
}

static void snapshot_timer_cb(TimerHandle_t handle) {
  (void)handle;
  /* Timer callbacks must be short and non-blocking: just signal the worker.
   * The capture (camera + SD) runs on the worker's 8 KB stack, not this 2 KB
   * Tmr Svc stack — running it here overflowed Tmr Svc and panicked. */
  snapshot_capture_async();
}

esp_err_t snapshot_init(void) {
  if (s_timer) {
    /* Already created; restart at the default period (or stop if off). */
    if (SNAPSHOT_INTERVAL_S > 0) {
      xTimerChangePeriod(s_timer, pdMS_TO_TICKS(SNAPSHOT_INTERVAL_S * 1000), 0);
    } else {
      xTimerStop(s_timer, 0);
    }
    return ESP_OK;
  }
  /* Create the capture worker BEFORE the timer so the first fire can't race a
   * missing worker. 8 KB stack (camera capture + SD), core 0, priority 4 —
   * matches the video task; below the audio encode task. */
  BaseType_t ok = xTaskCreatePinnedToCore(snapshot_worker_task, "snapwork",
                                          8192, NULL, 4, &s_worker, 0);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "xTaskCreatePinnedToCore(snapwork) failed");
    return ESP_FAIL;
  }
  /* xTimerCreate asserts period > 0. When the default is OFF we still create
   * the timer so snapshot_set_interval can enable it later — with a placeholder
   * period and no start. */
  TickType_t init_period = pdMS_TO_TICKS(SNAPSHOT_INTERVAL_S * 1000);
  if (init_period == 0) init_period = pdMS_TO_TICKS(1000);  /* placeholder; not started */
  s_timer = xTimerCreate("snap", init_period, pdTRUE, 0, snapshot_timer_cb);
  if (!s_timer) {
    ESP_LOGE(TAG, "xTimerCreate failed");
    return ESP_FAIL;
  }
  /* Start at the default interval. A 0 SNAPSHOT_INTERVAL_S means "off" — the
   * server turns it on later via snapshot_set_interval. */
  if (SNAPSHOT_INTERVAL_S > 0) {
    if (xTimerStart(s_timer, 0) != pdPASS) {
      ESP_LOGE(TAG, "xTimerStart failed");
      return ESP_FAIL;
    }
  }
  ESP_LOGI(TAG, "ambient snapshot timer %s",
           SNAPSHOT_INTERVAL_S > 0 ? "on" : "off (default; server/button can enable)");
  return ESP_OK;
}

void snapshot_set_interval(uint32_t seconds) {
  if (!s_timer) {
    ESP_LOGW(TAG, "set_interval before init — ignored");
    return;
  }
  if (seconds == 0) {
    xTimerStop(s_timer, 0);
    ESP_LOGI(TAG, "snapshot timer stopped");
    return;
  }
  /* xTimerChangePeriod also starts the timer if it was stopped. */
  xTimerChangePeriod(s_timer, pdMS_TO_TICKS(seconds * 1000), 0);
  ESP_LOGI(TAG, "snapshot interval -> %u s", (unsigned)seconds);
}