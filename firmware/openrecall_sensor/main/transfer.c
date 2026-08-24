/*
 * P4b WiFi transfer — see transfer.h. The transfer task brings up a SoftAP,
 * serves the manifest + item files + consumed-delete over HTTP, holds for the
 * bounded window, then tears down in the reverse order and resumes BLE +
 * ambient snapshot.
 *
 * WiFi init guard: esp_wifi_init + esp_netif_create_default_wifi_ap are called
 * exactly once (the audio/BLE path never uses WiFi). esp_wifi_start/stop are
 * per-window and can repeat.
 *
 * Coexistence: WiFi + BLE share the 2.4 GHz radio. ble_link_suspend() (which
 * stops adv + disconnects the phone) is called BEFORE esp_wifi_start so the
 * radio is free. ble_link_resume() is called AFTER esp_wifi_stop().
 */
#include "transfer.h"
#include "boot_id.h"
#include "ble_link.h"
#include "config.h"
#include "media_index.h"
#include "sd_store.h"
#include "snapshot.h"

#include "esp_http_server.h"
#include "esp_log.h"
#include "esp_netif.h"
#include "esp_wifi.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const char *TAG = "transfer";

/* ---- Module state ---- */

/* WiFi init guard — esp_wifi_init + netif create are one-shot. */
static bool s_wifi_inited = false;

/* v1 simplification: resume ambient at the config default (SNAPSHOT_INTERVAL_S).
 * If the server changed the interval via snapshot_set_interval before the flush,
 * we resume at the default rather than the server's value. See task-10 report. */
static uint32_t s_resume_interval = SNAPSHOT_INTERVAL_S;

/* Transfer task handle — re-entrancy guard + notification target for early
 * teardown. Set by transfer_flush (via xTaskCreate), cleared by transfer_task
 * before vTaskDelete. */
static TaskHandle_t s_transfer_task = NULL;

/* ---- Buffer sizes ---- */
#define MANIFEST_BUF_CAP    8192  /* ~80 manifest lines; plenty for a 600 s window */
#define CONSUMED_BODY_CAP   4096  /* newline-separated paths in POST /consumed */
#define CONSUMED_MAX_NAMES  32   /* max files deleted in one POST /consumed */
#define ITEM_CHUNK_SIZE     4096  /* streaming chunk for GET /item/{name} */
#define LINE_BUF_CAP        128   /* max manifest line length for parsing */

/* ---- HTTP handlers ---- */

/* GET /manifest — respond 200 text/plain with the raw manifest bytes.
 * Empty/missing manifest → 200 with empty body (valid state, not 404). */
static esp_err_t manifest_handler(httpd_req_t *req) {
  char *buf = malloc(MANIFEST_BUF_CAP);
  if (!buf) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    return ESP_OK;
  }
  size_t len = 0;
  esp_err_t err = sd_store_read_manifest(buf, MANIFEST_BUF_CAP, &len);
  if (err != ESP_OK) {
    free(buf);
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "manifest read failed");
    return ESP_OK;
  }
  httpd_resp_set_type(req, "text/plain");
  httpd_resp_send(req, buf, (ssize_t)len);
  free(buf);
  return ESP_OK;
}

/* GET /item/{name} — stream a file from the SD card in chunks. The {name} is
 * the full SD path (e.g. /sdcard/snapshots/7/0000005000_0.jpg) taken from the
 * manifest. Content-Type is sniffed from the extension. 404 if the file is
 * missing, 403 if outside /sdcard/. */
static esp_err_t item_handler(httpd_req_t *req) {
  /* Extract the path from /item/<path>. */
  const char *uri = req->uri;
  if (strncmp(uri, "/item/", 6) != 0) {
    httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "not found");
    return ESP_OK;
  }
  const char *name = uri + 6;
  size_t name_len = strlen(name);
  /* Trim query string if present. */
  const char *query = strchr(name, '?');
  if (query) name_len = (size_t)(query - name);
  if (name_len == 0 || name_len >= NAME_MAX_LEN) {
    httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "invalid name");
    return ESP_OK;
  }

  /* Build the full path — ensure it starts with '/'. */
  char path[NAME_MAX_LEN + 2];
  if (name[0] == '/') {
    memcpy(path, name, name_len);
    path[name_len] = '\0';
  } else {
    path[0] = '/';
    memcpy(path + 1, name, name_len);
    path[1 + name_len] = '\0';
  }

  /* Reject traversal — the /sdcard/ prefix check alone does NOT stop
   * "/sdcard/../" because such a path starts with "/sdcard/". */
  if (strstr(path, "..") != NULL) {
    httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "invalid path");
    return ESP_OK;
  }
  /* Only serve files under /sdcard/. */
  if (strncmp(path, "/sdcard/", 8) != 0) {
    httpd_resp_send_err(req, HTTPD_403_FORBIDDEN, "outside sdcard");
    return ESP_OK;
  }

  /* Allocate the chunk buffer before opening the file so OOM doesn't race
   * with content-type setting. */
  char *chunk = malloc(ITEM_CHUNK_SIZE);
  if (!chunk) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    return ESP_OK;
  }

  FILE *f = fopen(path, "rb");
  if (!f) {
    free(chunk);
    httpd_resp_send_err(req, HTTPD_404_NOT_FOUND, "file not found");
    return ESP_OK;
  }

  /* Content type by extension. */
  const char *ext = strrchr(path, '.');
  if (ext && (strcmp(ext, ".jpg") == 0 || strcmp(ext, ".jpeg") == 0)) {
    httpd_resp_set_type(req, "image/jpeg");
  } else if (ext && strcmp(ext, ".mjpeg") == 0) {
    httpd_resp_set_type(req, "video/x-mjpeg");
  } else {
    httpd_resp_set_type(req, "application/octet-stream");
  }

  /* Stream in chunks to bound memory (video files can be large). */
  size_t n;
  while ((n = fread(chunk, 1, ITEM_CHUNK_SIZE, f)) > 0) {
    if (httpd_resp_send_chunk(req, chunk, (ssize_t)n) != ESP_OK) {
      ESP_LOGE(TAG, "chunk send failed for %s", path);
      break;
    }
  }
  if (ferror(f)) {
    ESP_LOGE(TAG, "fread error streaming %s", path);
  }
  fclose(f);
  free(chunk);
  httpd_resp_send_chunk(req, NULL, 0);  /* end response */
  return ESP_OK;
}

/* POST /consumed — read newline-separated full paths, delete each file, then
 * trim the manifest (rewrite without the consumed lines). If the manifest is
 * now empty, signal early teardown via xTaskNotifyGive. Respond 200. */
static esp_err_t consumed_handler(httpd_req_t *req) {
  /* Read the body (newline-separated full paths). */
  char *body = malloc(CONSUMED_BODY_CAP);
  if (!body) {
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    return ESP_OK;
  }
  size_t total = 0;
  while (total < CONSUMED_BODY_CAP - 1) {
    int n = httpd_req_recv(req, body + total, CONSUMED_BODY_CAP - 1 - total);
    if (n <= 0) break;
    total += (size_t)n;
  }
  body[total] = '\0';
  if (total >= CONSUMED_BODY_CAP - 1 && req->content_len > (size_t)total) {
    ESP_LOGW(TAG, "consumed body truncated (%zu/%zu)", total,
             (size_t)req->content_len);
  }

  /* Parse the consumed names (full paths). */
  char names[CONSUMED_MAX_NAMES][NAME_MAX_LEN];
  size_t count = 0;
  if (consumed_parse(body, names, &count, CONSUMED_MAX_NAMES) != 0) {
    free(body);
    httpd_resp_send_err(req, HTTPD_400_BAD_REQUEST, "parse failed");
    return ESP_OK;
  }
  free(body);

  /* Delete each consumed file (idempotent — missing files are OK). */
  for (size_t i = 0; i < count; i++) {
    sd_store_delete(names[i]);
  }

  /* Trim the manifest: read, filter out consumed lines, rewrite. */
  char *manifest = malloc(MANIFEST_BUF_CAP);
  char *new_manifest = malloc(MANIFEST_BUF_CAP);
  if (!manifest || !new_manifest) {
    free(manifest);
    free(new_manifest);
    httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "oom");
    return ESP_OK;
  }

  size_t man_len = 0;
  sd_store_read_manifest(manifest, MANIFEST_BUF_CAP, &man_len);

  /* If we read exactly MANIFEST_BUF_CAP bytes and the last byte isn't '\n',
   * the manifest was likely truncated — skip the trim to avoid corruption. */
  bool truncated = (man_len == MANIFEST_BUF_CAP && man_len > 0 &&
                    manifest[man_len - 1] != '\n');

  size_t new_len = 0;
  if (!truncated) {
    char *p = manifest;
    char *end = manifest + man_len;
    char line_buf[LINE_BUF_CAP];
    while (p < end) {
      char *nl = memchr(p, '\n', (size_t)(end - p));
      size_t line_len = nl ? (size_t)(nl - p) : (size_t)(end - p);
      if (line_len > 0 && line_len < sizeof(line_buf)) {
        memcpy(line_buf, p, line_len);
        line_buf[line_len] = '\0';
        char path_out[NAME_MAX_LEN];
        if (manifest_parse(line_buf, path_out, sizeof(path_out),
                           NULL, NULL, NULL) == 0) {
          /* Check if this line's path was consumed. */
          bool consumed = false;
          for (size_t i = 0; i < count; i++) {
            if (strcmp(path_out, names[i]) == 0) {
              consumed = true;
              break;
            }
          }
          if (!consumed) {
            memcpy(new_manifest + new_len, p, line_len);
            new_len += line_len;
            new_manifest[new_len++] = '\n';
          }
        } else {
          /* Unparseable line — keep it verbatim. */
          memcpy(new_manifest + new_len, p, line_len);
          new_len += line_len;
          new_manifest[new_len++] = '\n';
        }
      } else if (line_len > 0) {
        /* Line too long for line_buf — keep it verbatim. */
        memcpy(new_manifest + new_len, p, line_len);
        new_len += line_len;
        new_manifest[new_len++] = '\n';
      }
      if (!nl) break;
      p = nl + 1;
    }
    if (sd_store_write(SD_MANIFEST, (const uint8_t *)new_manifest, new_len) != ESP_OK) {
      free(manifest);
      free(new_manifest);
      httpd_resp_send_err(req, HTTPD_500_INTERNAL_SERVER_ERROR, "manifest rewrite failed");
      return ESP_OK;
    }
  } else {
    ESP_LOGW(TAG, "manifest truncated (%zu bytes) — skip trim", man_len);
    new_len = man_len;  /* don't trigger early teardown on a truncated read */
  }

  free(manifest);
  free(new_manifest);

  /* Early teardown if the manifest is now empty. Read the task handle once
   * into a local to avoid a TOCTOU race: the transfer task is alive while the
   * httpd server is running (httpd_stop waits for handlers to finish), so the
   * handle is valid when we use it. */
  if (new_len == 0) {
    TaskHandle_t task = s_transfer_task;
    if (task) {
      xTaskNotifyGive(task);
    }
  }

  httpd_resp_send(req, "OK", 2);
  return ESP_OK;
}

/* ---- Transfer task ---- */

static void transfer_task(void *arg) {
  (void)arg;
  httpd_handle_t server = NULL;
  bool wifi_started = false;
  bool ble_suspended = false;
  esp_err_t err;

  /* 1. Stop ambient capture (camera stays init'd; the timer stops). */
  snapshot_set_interval(0);

  /* 2. Suspend BLE — frees the 2.4 GHz radio for WiFi. Safe even if BLE was
   *    never started (adv_stop is a no-op, no conn means no terminate). */
  ble_link_suspend();
  ble_suspended = true;

  /* 3. Ensure SD is mounted (manifest + item reads need the VFS). */
  err = sd_store_mount();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "sd_store_mount: %s", esp_err_to_name(err));
    goto teardown;
  }

  /* 4. Init WiFi once (the audio/BLE path never uses WiFi). Init WiFi first,
   *    then create the netif — if wifi_init fails, no netif is leaked. */
  if (!s_wifi_inited) {
    wifi_init_config_t cfg = WIFI_INIT_CONFIG_DEFAULT();
    err = esp_wifi_init(&cfg);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "esp_wifi_init: %s", esp_err_to_name(err));
      goto teardown;
    }
    esp_netif_t *netif = esp_netif_create_default_wifi_ap();
    if (!netif) {
      ESP_LOGE(TAG, "esp_netif_create_default_wifi_ap failed");
      /* esp_wifi_init already succeeded above; deinit so the WiFi state is
       * clean for a retry. Without this, s_wifi_inited stays false (set only
       * after both succeed) but WiFi is init'd — the next transfer_flush would
       * re-call esp_wifi_init on already-init'd WiFi → ESP_ERR_INVALID_STATE. */
      esp_wifi_deinit();
      goto teardown;
    }
    s_wifi_inited = true;
  }

  /* 5. Configure + start SoftAP. SSID = prefix + boot_id (e.g. OpenRecall-7). */
  wifi_config_t wifi_cfg = {
    .ap = {
      .channel = SOFTAP_CHANNEL,
      .authmode = WIFI_AUTH_OPEN,
      .max_connection = 1,
    },
  };
  char ssid[32];
  snprintf(ssid, sizeof(ssid), SOFTAP_SSID_PREFIX "%lu",
           (unsigned long)boot_id_get());
  strncpy((char *)wifi_cfg.ap.ssid, ssid, sizeof(wifi_cfg.ap.ssid));
  wifi_cfg.ap.ssid_len = (uint8_t)strlen(ssid);

  err = esp_wifi_set_mode(WIFI_MODE_AP);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_wifi_set_mode: %s", esp_err_to_name(err));
    goto teardown;
  }
  err = esp_wifi_set_config(WIFI_IF_AP, &wifi_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_wifi_set_config: %s", esp_err_to_name(err));
    goto teardown;
  }
  err = esp_wifi_start();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_wifi_start: %s", esp_err_to_name(err));
    goto teardown;
  }
  wifi_started = true;
  ESP_LOGI(TAG, "SoftAP up: SSID=%s ch=%d", ssid, SOFTAP_CHANNEL);

  /* 6. Start HTTP server with three handlers. */
  httpd_config_t http_cfg = HTTPD_DEFAULT_CONFIG();
  http_cfg.server_port = TRANSFER_HTTP_PORT;
  http_cfg.max_uri_handlers = 3;
  http_cfg.stack_size = 8192;
  http_cfg.uri_match_fn = httpd_uri_match_wildcard;  /* enables /item/ wildcard */
  err = httpd_start(&server, &http_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "httpd_start: %s", esp_err_to_name(err));
    goto teardown;
  }

  httpd_uri_t manifest_uri = {
    .uri = "/manifest", .method = HTTP_GET, .handler = manifest_handler
  };
  httpd_uri_t item_uri = {
    .uri = "/item/*", .method = HTTP_GET, .handler = item_handler
  };
  httpd_uri_t consumed_uri = {
    .uri = "/consumed", .method = HTTP_POST, .handler = consumed_handler
  };
  httpd_register_uri_handler(server, &manifest_uri);
  httpd_register_uri_handler(server, &item_uri);
  httpd_register_uri_handler(server, &consumed_uri);

  ESP_LOGI(TAG, "HTTP server on port %d — window %d s", TRANSFER_HTTP_PORT,
           WIFI_TRANSFER_WINDOW_S);

  /* 7. Hold for WIFI_TRANSFER_WINDOW_S or until the manifest empties.
   *    POST /consumed signals via xTaskNotifyGive when the manifest is empty.
   *    ulTaskNotifyTake with a 1 s timeout wakes immediately on the notify;
   *    otherwise it times out each second and decrements the counter. */
  (void)ulTaskNotifyTake(pdTRUE, 0);  /* clear any stale notification */
  for (int remaining = WIFI_TRANSFER_WINDOW_S; remaining > 0; remaining--) {
    uint32_t notify = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1000));
    if (notify > 0) {
      ESP_LOGI(TAG, "early teardown: manifest empty");
      break;
    }
  }

teardown:
  /* Teardown order (spec §3.2): HTTP → WiFi → BLE → snapshot. Each step is
   * best-effort so a failure in one doesn't skip the rest. */
  if (server) {
    esp_err_t e = httpd_stop(server);
    if (e != ESP_OK) ESP_LOGE(TAG, "httpd_stop: %s", esp_err_to_name(e));
  }
  if (wifi_started) {
    esp_err_t e = esp_wifi_stop();
    if (e != ESP_OK) ESP_LOGE(TAG, "esp_wifi_stop: %s", esp_err_to_name(e));
  }
  if (ble_suspended) {
    ble_link_resume();
  }
  snapshot_set_interval(s_resume_interval);
  s_transfer_task = NULL;
  ESP_LOGI(TAG, "transfer window closed");
  vTaskDelete(NULL);
}

/* ---- Public API ---- */

esp_err_t transfer_flush(void) {
  if (s_transfer_task) {
    ESP_LOGW(TAG, "transfer already in progress");
    return ESP_ERR_INVALID_STATE;
  }
  BaseType_t ok = xTaskCreate(transfer_task, "transfer", TRANSFER_TASK_STACK,
                              NULL, TRANSFER_TASK_PRIO, &s_transfer_task);
  if (ok != pdPASS) {
    ESP_LOGE(TAG, "xTaskCreate failed");
    s_transfer_task = NULL;
    return ESP_FAIL;
  }
  return ESP_OK;
}
