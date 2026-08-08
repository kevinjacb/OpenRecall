/*
 * Portable executor core — command parse/validate + request_buffer window math.
 *
 * No ESP/FreeRTOS headers: compiles on the host (cJSON + config.h) so the
 * validation logic is unit-tested without the ESP toolchain (test/test_executors.c).
 * The ESP-only wiring (queue, task, dispatch) lives in executor.{h,c}.
 *
 * Validation mirrors the server's _TYPE_SCHEMAS (opensapien_server/agent/validator_command.py):
 * unknown param keys are rejected, required keys must be present, numbers must
 * not be booleans, and each field is bound-checked. This is defense-in-depth —
 * the server already validates before issuing; the device re-checks so a relay
 * bug or a future issuer can't drive a bad param into a handler.
 */
#pragma once

#include <stddef.h>
#include <stdint.h>
#include <string.h>

#include "cJSON.h"

#ifdef __cplusplus
extern "C" {
#endif

typedef enum {
  EXEC_OK = 0,
  EXEC_BAD_PARAMS = 1,
  EXEC_UNKNOWN_TYPE = 2,
} executor_status_t;

typedef enum {
  CMD_UNKNOWN = 0,
  CMD_START_AUDIO,
  CMD_STOP_AUDIO,
  CMD_REQUEST_BUFFER,
  CMD_CAPTURE_PHOTO,
  CMD_RECORD_VIDEO,
} cmd_type_t;

typedef struct {
  cmd_type_t        type;
  executor_status_t status;
  uint32_t          seconds;     /* request_buffer (validated)        */
  uint32_t          duration_s;  /* record_video   (validated; P4b)   */
} cmd_request_t;

/* Parse `type` + `params` (may be NULL) into `out`, setting out->status.
   On EXEC_OK, out->type + the relevant field are filled. */
void executor_parse_and_validate(const char *type, const cJSON *params, cmd_request_t *out);

/* Compute the request_buffer replay window: the last `seconds` of ring frames.
   N = min(seconds*50, RING_FRAMES, write_idx); *start_idx = write_idx - N. */
void replay_window(uint32_t seconds, uint32_t write_idx, uint32_t *start_idx, uint32_t *n);

#ifdef __cplusplus
}
#endif