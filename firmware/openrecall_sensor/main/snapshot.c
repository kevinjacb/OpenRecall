/*
 * P4b ambient snapshot — see snapshot.h. The timer callback runs on the
 * FreeRTOS timer service task (core 0); it captures one JPEG, writes it to
 * SD, and appends a manifest line. The SD write (~92 ms worst case, Spike-2)
 * happens on the timer task, not the audio real-time path, so it cannot stall
 * §C.6. rel_ts_ms_now() reads a volatile uint32 updated by the audio task each
 * frame — a 32-bit aligned read is atomic on the ESP32, sufficient for a ms
 * clock shared between cores.
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
static uint32_t s_seq = 0;   /* monotonic per-boot counter */

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
  /* Self-contained on the timer service task (core 0). rel_ts_ms_now() reads
   * the audio task's clock without blocking it. */
  (void)snapshot_capture_one(rel_ts_ms_now());
}

esp_err_t snapshot_init(void) {
  if (s_timer) {
    /* Already created; restart at the default period. */
    xTimerChangePeriod(s_timer, pdMS_TO_TICKS(SNAPSHOT_INTERVAL_S * 1000), 0);
    return ESP_OK;
  }
  s_timer = xTimerCreate("snap", pdMS_TO_TICKS(SNAPSHOT_INTERVAL_S * 1000),
                         pdTRUE, 0, snapshot_timer_cb);
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
  ESP_LOGI(TAG, "ambient snapshot timer @ %d s", (int)SNAPSHOT_INTERVAL_S);
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