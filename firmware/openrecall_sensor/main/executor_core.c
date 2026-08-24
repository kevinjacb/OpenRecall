#include "executor_core.h"
#include "config.h"

#include <stdbool.h>

/* True if `params` (may be NULL) contains only keys in `allowed` (NULL-terminated). */
static bool params_only_allowed(const cJSON *params, const char *const *allowed) {
  if (params == NULL || !cJSON_IsObject(params)) return true;  /* NULL/missing == empty */
  for (cJSON *child = params->child; child != NULL; child = child->next) {
    bool ok = false;
    for (const char *const *k = allowed; *k != NULL; k++) {
      if (strcmp(child->string, *k) == 0) { ok = true; break; }
    }
    if (!ok) return false;
  }
  return true;
}

/* Fetch a numeric (int/float, NOT bool) field. Returns false if missing/non-numeric/bool. */
static bool get_number(const cJSON *params, const char *key, double *out) {
  if (params == NULL) return false;
  const cJSON *node = cJSON_GetObjectItemCaseSensitive(params, key);
  if (node == NULL || !cJSON_IsNumber(node)) return false;  /* cJSON_IsNumber is false for True/False */
  *out = node->valuedouble;
  return true;
}

void executor_parse_and_validate(const char *type, const cJSON *params, cmd_request_t *out) {
  memset(out, 0, sizeof *out);
  out->status = EXEC_OK;

  if (strcmp(type, "start_audio") == 0) {
    out->type = CMD_START_AUDIO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "stop_audio") == 0) {
    out->type = CMD_STOP_AUDIO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "capture_photo") == 0) {
    out->type = CMD_CAPTURE_PHOTO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "record_video") == 0) {
    out->type = CMD_RECORD_VIDEO;
    static const char *const allowed[] = { "duration_s", NULL };
    if (!params_only_allowed(params, allowed)) { out->status = EXEC_BAD_PARAMS; return; }
    double d;
    if (!get_number(params, "duration_s", &d)) { out->status = EXEC_BAD_PARAMS; return; }
    if (d < 1.0 || d > 30.0) { out->status = EXEC_BAD_PARAMS; return; }
    out->duration_s = (uint32_t)d;
    return;
  }
  if (strcmp(type, "request_buffer") == 0) {
    out->type = CMD_REQUEST_BUFFER;
    static const char *const allowed[] = { "seconds", NULL };
    if (!params_only_allowed(params, allowed)) { out->status = EXEC_BAD_PARAMS; return; }
    double s;
    if (!get_number(params, "seconds", &s)) { out->status = EXEC_BAD_PARAMS; return; }
    if (s < (double)REQ_BUFFER_MIN_SECONDS || s > (double)REQ_BUFFER_MAX_SECONDS) {
      out->status = EXEC_BAD_PARAMS;
      return;
    }
    out->seconds = (uint32_t)s;
    return;
  }
  if (strcmp(type, "start_video") == 0) {
    out->type = CMD_START_VIDEO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "stop_video") == 0) {
    out->type = CMD_STOP_VIDEO;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "flush_snapshots") == 0) {
    out->type = CMD_FLUSH_SNAPSHOTS;
    static const char *const allowed[] = { NULL };
    if (!params_only_allowed(params, allowed)) out->status = EXEC_BAD_PARAMS;
    return;
  }
  if (strcmp(type, "set_snapshot_interval") == 0) {
    out->type = CMD_SET_SNAPSHOT_INTERVAL;
    static const char *const allowed[] = { "seconds", NULL };
    if (!params_only_allowed(params, allowed)) { out->status = EXEC_BAD_PARAMS; return; }
    double s;
    if (!get_number(params, "seconds", &s)) { out->status = EXEC_BAD_PARAMS; return; }
    if (s < (double)SNAPSHOT_INTERVAL_MIN || s > (double)SNAPSHOT_INTERVAL_MAX) {
      out->status = EXEC_BAD_PARAMS; return;
    }
    out->snapshot_interval_s = (uint32_t)s;
    return;
  }
  /* play_audio / display_text / show_status / anything else */
  out->type = CMD_UNKNOWN;
  out->status = EXEC_UNKNOWN_TYPE;
}

void replay_window(uint32_t seconds, uint32_t write_idx, uint32_t *start_idx, uint32_t *n) {
  uint32_t want = seconds * (1000u / FRAME_MS);   /* seconds * 50 */
  uint32_t N = want;
  if (N > RING_FRAMES) N = RING_FRAMES;
  if (N > write_idx)   N = write_idx;
  *start_idx = write_idx - N;
  *n = N;
}