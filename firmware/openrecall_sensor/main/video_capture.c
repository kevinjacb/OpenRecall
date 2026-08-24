/*
 * P4b continuous video — see video_capture.h.
 *
 * Clip file lifecycle: the clip is kept open for the whole recording. video_start
 * opens s_path with fopen("wb") (create/truncate, handle kept in s_file); the
 * video task fwrites JPEGs into s_file; video_stop closes it. sd_store_write/
 * sd_store_append open+close the file each call, so they cannot be used for the
 * clip body (we want one long-lived handle). sd_store_append is used ONLY for the
 * manifest line, which is line-oriented and fine for that.
 *
 * Stop synchronization: FreeRTOS has no xTaskJoin. video_stop sets s_recording
 * = false, stores its own task handle in s_stop_waiter, and blocks on
 * ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5000)) — a bounded binary-semaphore-
 * style wait that clears the notification value on exit. The video task, after
 * it observes s_recording == false, does its final flush + fclose(s_file) +
 * manifest append, then calls xTaskNotifyGive(s_stop_waiter) to release
 * video_stop, clears s_task = NULL, and vTaskDelete(NULL). The wait is BOUNDED
 * (not portMAX_DELAY) because camera_capture_jpeg is an unbounded blocking call
 * (esp32-camera waits on a frame-buffer semaphore with no timeout; an SCCB/I2C
 * bus hang is a known failure mode) — if the task is stuck inside it when stop
 * fires, an unbounded wait here would hang the system silently. On 5 s timeout
 * video_stop logs and returns anyway (the clip may be incomplete, which is
 * acceptable; the system stays responsive). Normally finalization takes ~100 ms.
 *
 * Thread-safety: s_recording is volatile bool — single producer (video_stop sets
 * it false) and one reader (the video task loop) on a core-0-pinned task; no
 * atomic needed beyond volatile. s_seq is touched only by video_start (the
 * start/record path), not by the video task loop. s_file/s_path are set by
 * video_start before s_recording=true and the task is created, so the task
 * always sees a consistent file handle.
 */
#include "video_capture.h"
#include "boot_id.h"
#include "camera.h"
#include "config.h"
#include "media_index.h"
#include "sd_store.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <errno.h>
#include <inttypes.h>  /* PRIu32 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

static const char *TAG = "video";

/* ---- State (see header comment for the synchronization model) ---- */
static volatile bool s_recording = false;
static TaskHandle_t   s_task = NULL;        /* the video_task handle */
static volatile TaskHandle_t s_stop_waiter = NULL; /* video_stop's task, signaled on completion */
static FILE          *s_file = NULL;        /* kept open for the whole clip */
static char           s_path[NAME_MAX_LEN];
static uint32_t       s_seq = 0;            /* monotonic per-boot clip counter */
static uint32_t       s_start_rel_ts_ms = 0; /* rel_ts_ms captured at start, for the manifest line */

/* One-shot mkdir that treats "already exists" as success. The per-boot video
 * subdir (/sdcard/video/<boot_id>) is not created by sd_store_mount (only the
 * top-level /sdcard/video is), so ensure it before opening the clip. Replicated
 * from snapshot.c so this module stays self-contained. */
static esp_err_t ensure_dir(const char *path) {
  if (mkdir(path, 0777) == 0) return ESP_OK;
  if (errno == EEXIST) return ESP_OK;
  ESP_LOGE(TAG, "mkdir(%s): %s", path, strerror(errno));
  return ESP_FAIL;
}

/* ---- The capture loop ---- */
static void video_task(void *arg) {
  (void)arg;
  uint32_t frame = 0;
  const uint32_t frame_period_ms = 1000 / VIDEO_FPS;  /* target period */

  while (s_recording) {
    uint8_t *jpg = NULL;
    size_t   n = 0;
    esp_err_t err = camera_capture_jpeg(&jpg, &n);
    if (err != ESP_OK || jpg == NULL || n == 0) {
      /* A dropped frame is acceptable; a corrupt clip is not. Log and continue.
       * free(jpg) unconditionally: camera_capture_jpeg's contract does not
       * guarantee *out_buf==NULL on error, and an ESP_OK + n==0 frame can still
       * carry a non-NULL buffer. free(NULL) is well-defined, so this is safe
       * regardless of which sub-condition fired — and it prevents PSRAM leaking
       * across repeated dropped frames (which would eventually crash the device). */
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "camera_capture_jpeg: %s", esp_err_to_name(err));
      } else if (n == 0) {
        ESP_LOGW(TAG, "camera_capture_jpeg: empty frame, skipping");
      }
      free(jpg);
      vTaskDelay(pdMS_TO_TICKS(frame_period_ms));
      continue;
    }

    /* MJPEG = concatenation of JPEGs. fwrite directly into the kept-open clip. */
    size_t wrote = fwrite(jpg, 1, n, s_file);
    free(jpg);
    if (wrote != n) {
      /* Short write — SD full or I/O error. Log and continue; do not abort the
       * whole clip over one frame (the server's split_mjpeg tolerates a ragged
       * tail). fclose on stop will still finalize whatever was written. */
      ESP_LOGE(TAG, "fwrite short: wrote %zu of %zu — frame dropped", wrote, n);
    }

    /* Flush roughly every 1 s to bound PSRAM burst memory and SD write latency
     * backing up into the camera fb double-buffer. fclose on stop does a final
     * flush. */
    if (++frame % VIDEO_FPS == 0) {
      fflush(s_file);
    }

    vTaskDelay(pdMS_TO_TICKS(frame_period_ms));
  }

  /* ---- Teardown (s_recording was set false by video_stop) ----
   * Finalize the clip BEFORE signaling completion: flush, close the file, then
   * append the manifest line (kind 'v'). Only once the clip is finalized do we
   * xTaskNotifyGive(s_stop_waiter) so video_stop returns. */
  if (s_file) {
    fflush(s_file);
    if (fclose(s_file) != 0) {
      ESP_LOGE(TAG, "fclose(%s): %s", s_path, strerror(errno));
    }
    s_file = NULL;
  }

  /* Append the manifest line now that the file is closed and complete. Use the
   * rel_ts_ms captured at video_start so the manifest timestamps the clip start
   * (consistent with snapshot_capture_one, which stamps capture time). */
  char line[NAME_MAX_LEN + 32];
  manifest_line(line, sizeof line, s_path, s_start_rel_ts_ms, boot_id_get(), 'v');
  esp_err_t merr = sd_store_append(SD_MANIFEST, line);
  if (merr != ESP_OK) {
    ESP_LOGE(TAG, "sd_store_append(manifest): %s — clip file exists but is unlisted",
             esp_err_to_name(merr));
    /* Not fatal: the .mjpeg is on the card; the manifest is just missing this
     * entry until a later capture re-appends. */
  }

  ESP_LOGI(TAG, "clip done %s ts=%" PRIu32, s_path, s_start_rel_ts_ms);

  /* Release video_stop (it is blocked on ulTaskNotifyTake), then self-delete.
   * s_stop_waiter is read here only after video_stop set it and then blocked,
   * so the read is safe. Clear s_task before deleting so video_stop's NULL
   * check and a subsequent video_start see a clean slate. */
  TaskHandle_t waiter = s_stop_waiter;
  s_task = NULL;
  s_recording = false;  /* belt-and-braces; video_stop already set this */
  if (waiter) {
    xTaskNotifyGive(waiter);
  }
  vTaskDelete(NULL);
  /* never reached */
}

/* ---- record_video: start -> delay(duration) -> stop on a short-lived task ---- */
struct record_arg {
  uint32_t duration_s;
  uint32_t rel_ts_ms;
};

static void record_task(void *arg) {
  struct record_arg *a = (struct record_arg *)arg;
  esp_err_t err = video_start(a->rel_ts_ms);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "record: video_start failed: %s", esp_err_to_name(err));
    free(a);
    vTaskDelete(NULL);
    return;
  }
  vTaskDelay(pdMS_TO_TICKS(a->duration_s * 1000));
  video_stop();
  free(a);
  vTaskDelete(NULL);
}

/* ---- Public API ---- */
esp_err_t video_start(uint32_t rel_ts_ms) {
  if (s_recording) {
    ESP_LOGW(TAG, "start while already recording — ignored");
    return ESP_OK;
  }

  esp_err_t err = sd_store_mount();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "sd_store_mount: %s", esp_err_to_name(err));
    return err;
  }

  uint32_t boot = boot_id_get();
  uint32_t seq  = s_seq++;
  video_path(s_path, sizeof s_path, boot, rel_ts_ms, seq);
  s_start_rel_ts_ms = rel_ts_ms;

  /* Ensure the per-boot video subdir exists before opening the clip. Uses the
   * SD_VIDEO_DIR macro (single source of truth in config.h) with C string-literal
   * concatenation; matches what video_path() in media_index.c emits. */
  char dir[NAME_MAX_LEN];
  snprintf(dir, sizeof dir, SD_VIDEO_DIR "/%" PRIu32, boot);
  (void)ensure_dir(dir);

  /* Open the clip ONCE and keep the handle for the whole recording. sd_store_write
   * opens+ closes per call so it cannot create a file we then keep open. */
  s_file = fopen(s_path, "wb");
  if (!s_file) {
    ESP_LOGE(TAG, "fopen(%s, wb): %s", s_path, strerror(errno));
    /* Do NOT set s_recording — start failed. */
    return ESP_FAIL;
  }

  s_recording = true;
  s_stop_waiter = NULL;

  /* Pinned to core 0 at priority 4 — BELOW the BLE audio drainer (the hard
   * real-time constraint). The video task's vTaskDelay(1000/VIDEO_FPS) yields
   * each frame so the drainer's BLE notify deadline can be met. If a real
   * collision shows up as audio chunk_seq gaps during a recording in the final
   * smoke, TODO: lower VIDEO_FPS or raise the drainer priority. */
  BaseType_t ok = xTaskCreatePinnedToCore(video_task, "video", 8192, NULL,
                                          4, &s_task, 0);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "xTaskCreatePinnedToCore(video) failed");
    fclose(s_file);
    s_file = NULL;
    s_recording = false;
    return ESP_FAIL;
  }

  ESP_LOGI(TAG, "recording %s @ %d fps (boot=%" PRIu32 " seq=%" PRIu32 ")",
           s_path, (int)VIDEO_FPS, boot, seq);
  return ESP_OK;
}

void video_stop(void) {
  if (!s_recording) {
    /* Safe to call when not recording — no-op. Also covers the case where the
     * task was never started (s_task == NULL). */
    return;
  }

  /* Record which task is waiting, then drop the flag so the video task exits its
   * loop and finalizes the clip. */
  s_stop_waiter = xTaskGetCurrentTaskHandle();
  s_recording = false;

  /* Drain any stale notification already pending on this task so a stray
   * xTaskNotifyGive from elsewhere can't make video_stop return prematurely. */
  (void)ulTaskNotifyTake(pdTRUE, 0);

  /* Block until the video task has flushed, closed the file, and appended the
   * manifest line. Bounded wait (NOT portMAX_DELAY): camera_capture_jpeg is an
   * UNBOUNDED blocking call — the esp32-camera driver waits on a frame-buffer
   * semaphore with no timeout, and an SCCB/I2C bus hang is a known failure mode.
   * If the video task is stuck inside camera_capture_jpeg when we flip
   * s_recording=false, it never finalizes, never notifies, and an unbounded
   * wait here would hang the system silently (only a watchdog reboot recovers).
   * 5 s is far longer than normal finalization (~one frame period + flush +
   * fclose + manifest ≈ 100 ms). On timeout we log and return anyway — the
   * system stays responsive; the clip may be incomplete, which is acceptable. */
  uint32_t got = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(5000));
  if (got == 0) {
    ESP_LOGE(TAG, "video_stop: video task did not finalize within 5000 ms — clip may be incomplete");
  }
  s_stop_waiter = NULL;
}

esp_err_t video_record(uint32_t duration_s, uint32_t rel_ts_ms) {
  /* Clamp to a sane max. The executor_core parser already bounds this, but a
   * defensive clamp + log keeps a misrouted call from pinning a task for minutes. */
  const uint32_t MAX_DURATION_S = 120;
  if (duration_s == 0) {
    ESP_LOGW(TAG, "record_video: duration=0 rejected");
    return ESP_ERR_INVALID_ARG;
  }
  if (duration_s > MAX_DURATION_S) {
    ESP_LOGW(TAG, "record_video duration=%" PRIu32 " > %u — clamped",
             duration_s, (unsigned)MAX_DURATION_S);
    duration_s = MAX_DURATION_S;
  }

  /* record_task runs start->delay->stop on a short-lived task so video_record
   * itself does not block the executor task that dispatched it. The record task
   * frees arg and self-deletes; video_record does not join it. */
  struct record_arg *arg = malloc(sizeof *arg);
  if (!arg) {
    ESP_LOGE(TAG, "malloc(record_arg) failed");
    return ESP_ERR_NO_MEM;
  }
  arg->duration_s = duration_s;
  arg->rel_ts_ms  = rel_ts_ms;

  BaseType_t ok = xTaskCreatePinnedToCore(record_task, "record", 4096, arg,
                                          4, NULL, 0);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "xTaskCreatePinnedToCore(record) failed");
    free(arg);
    return ESP_FAIL;
  }
  return ESP_OK;
}

bool video_is_recording(void) {
  return s_recording;
}
