/*
 * Host-side unit tests for the portable executor core (no ESP toolchain).
 *   cc -std=c11 -Wall -Wextra -I../main -I../managed_components/espressif__cjson/cJSON \
 *      ../managed_components/espressif__cjson/cJSON/cJSON.c ../main/executor_core.c \
 *      test_executors.c -o /tmp/exectest && /tmp/exectest
 *
 * Mirrors the server's _TYPE_SCHEMAS (validator_command.py): unknown-param
 * rejection, required-present, number-not-bool, bound checks.
 */
#include "executor_core.h"
#include "config.h"

#include <assert.h>
#include <stdio.h>
#include <string.h>

static int failures = 0;
static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

/* ---- executor_parse_and_validate ---- */

static cmd_request_t parse(const char *type, const char *params_json) {
  cJSON *p = params_json ? cJSON_Parse(params_json) : NULL;
  cmd_request_t req;
  executor_parse_and_validate(type, p, &req);
  if (p) cJSON_Delete(p);
  return req;
}

static void test_request_buffer(void) {
  printf("test_request_buffer\n");
  cmd_request_t r;
  r = parse("request_buffer", "{\"seconds\":1}");    check("min 1 ok",     r.status==EXEC_OK && r.type==CMD_REQUEST_BUFFER && r.seconds==1);
  r = parse("request_buffer", "{\"seconds\":30}");   check("30 ok",        r.status==EXEC_OK && r.seconds==30);
  r = parse("request_buffer", "{\"seconds\":60}");   check("max 60 ok",     r.status==EXEC_OK && r.seconds==60);
  r = parse("request_buffer", "{}");                 check("missing -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", NULL);                 check("null params -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":\"5\"}"); check("string -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":true}");  check("bool -> bad",   r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":0}");    check("below min -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":61}");   check("above max -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("request_buffer", "{\"seconds\":5.0}"); check("float ok",     r.status==EXEC_OK && r.seconds==5);
  r = parse("request_buffer", "{\"seconds\":5,\"x\":1}"); check("unknown param -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_start_stop_audio(void) {
  printf("test_start_stop_audio\n");
  cmd_request_t r;
  r = parse("start_audio", NULL);   check("start_audio no params ok", r.status==EXEC_OK && r.type==CMD_START_AUDIO);
  r = parse("start_audio", "{}");    check("start_audio empty ok",      r.status==EXEC_OK);
  r = parse("start_audio", "{\"x\":1}"); check("start_audio unknown -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("stop_audio", NULL);     check("stop_audio no params ok",  r.status==EXEC_OK && r.type==CMD_STOP_AUDIO);
  r = parse("stop_audio", "{\"q\":1}");  check("stop_audio unknown -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_photo_video_classify(void) {
  printf("test_photo_video_classify\n");
  cmd_request_t r;
  /* photo: no params; OK (no-op in P4a, but classification must be right for P4b) */
  r = parse("capture_photo", NULL); check("photo ok", r.status==EXEC_OK && r.type==CMD_CAPTURE_PHOTO);
  r = parse("capture_photo", "{\"x\":1}"); check("photo unknown -> bad", r.status==EXEC_BAD_PARAMS);
  /* video: duration_s required + bounded */
  r = parse("record_video", "{\"duration_s\":1}");  check("video min ok", r.status==EXEC_OK && r.type==CMD_RECORD_VIDEO && r.duration_s==1);
  r = parse("record_video", "{\"duration_s\":30}"); check("video max ok", r.status==EXEC_OK && r.duration_s==30);
  r = parse("record_video", "{}");                  check("video missing -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":0}");   check("video below -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":31}");  check("video above -> bad", r.status==EXEC_BAD_PARAMS);
  r = parse("record_video", "{\"duration_s\":5,\"y\":1}"); check("video unknown -> bad", r.status==EXEC_BAD_PARAMS);
}

static void test_unknown_type(void) {
  printf("test_unknown_type\n");
  cmd_request_t r;
  r = parse("play_audio", NULL);    check("play_audio unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
  r = parse("display_text", NULL);  check("display_text unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
  r = parse("nonsense", NULL);      check("nonsense unknown", r.status==EXEC_UNKNOWN_TYPE && r.type==CMD_UNKNOWN);
}

/* ---- replay_window ---- */

static void test_replay_window(void) {
  printf("test_replay_window\n");
  uint32_t s, n;
  replay_window(5, 10000, &s, &n); check("nominal 5s", s==9750 && n==250);
  replay_window(1, 10000, &s, &n); check("1s -> 50 frames", n==50);
  replay_window(60, 10000, &s, &n); check("60s -> RING_FRAMES", n==RING_FRAMES && s==10000-RING_FRAMES);
  replay_window(60, 200, &s, &n);   check("early boot cap", n==200 && s==0);
  replay_window(1, 0, &s, &n);      check("zero write_idx", n==0 && s==0);
}

int main(void) {
  test_request_buffer();
  test_start_stop_audio();
  test_photo_video_classify();
  test_unknown_type();
  test_replay_window();
  if (failures) { printf("FAIL: %d check(s)\n", failures); return 1; }
  printf("ALL PASS\n");
  return 0;
}