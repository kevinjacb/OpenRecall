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

#ifdef __cplusplus
}
#endif