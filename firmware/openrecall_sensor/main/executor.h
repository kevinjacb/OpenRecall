/*
 * §D command executor — ESP-only wiring (queue + task + dispatch).
 *
 * commands_handle (NimBLE host task) calls executor_submit, which parse/validates
 * (executor_core.c, host-tested) and enqueues a cmd_request_t to a FreeRTOS queue.
 * The executor task dequeues and dispatches to per-type handlers. executor_submit
 * returns true only on successful enqueue — commands_handle acks only then, so a
 * full queue is an honest "not accepted" (server re-issues) rather than a lost ack.
 */
#pragma once

#include "esp_err.h"
#include <stdbool.h>

#include "executor_core.h"   // cJSON via executor_core.h
#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Create the executor queue + task. Call once from app_main after commands_init. */
esp_err_t executor_init(void);

/* Parse+validate then enqueue. Returns true on successful enqueue (ack),
   false if the queue is full (no ack). Runs on the NimBLE host task. */
bool executor_submit(const char *type, const cJSON *params);

/* Enqueue a pre-built, already-validated request directly (no parse). For
   local issuers that build a cmd_request_t themselves — the D1 button has no
   cJSON, so it bypasses executor_submit. The caller must set req.status =
   EXEC_OK and fill the relevant field. Returns true on enqueue. */
bool executor_dispatch_local(const cmd_request_t *req);

#ifdef __cplusplus
}
#endif