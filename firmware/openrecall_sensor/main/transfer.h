/*
 * P4b WiFi transfer — bounded SoftAP window for end-of-day video retrieval.
 *
 * Transport split (spec §3.2): BLE for all real-time (audio/control/commands);
 * WiFi ONLY for this bounded transfer window. transfer_flush() is the
 * flush_snapshots command handler body. It spawns a short-lived task so it
 * doesn't block the executor: the task brings up a SoftAP, runs an HTTP server
 * with three endpoints (GET /manifest, GET /item/{name}, POST /consumed), holds
 * for WIFI_TRANSFER_WINDOW_S (or until the manifest empties), then tears down
 * and resumes BLE + ambient capture.
 */
#pragma once
#include "esp_err.h"

esp_err_t transfer_flush(void);   /* flush_snapshots handler body (spawns a task) */
