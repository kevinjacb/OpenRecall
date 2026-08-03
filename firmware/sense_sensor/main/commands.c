#include "commands.h"

#include "cJSON.h"
#include "esp_log.h"
#include "executor.h"
#include "sodium.h"

#include <stdio.h>
#include <string.h>

static const char *TAG = "cmd";

#define ED25519_SIG_LEN 64
#define CMD_DEDUPE_N    16   // remember recent command_ids for idempotency
#define CMD_ID_MAX      64

static uint8_t s_pubkey[32];
static command_ack_fn s_ack;
static char s_seen[CMD_DEDUPE_N][CMD_ID_MAX];
static int s_seen_head;

int commands_init(const uint8_t server_pubkey[32], command_ack_fn ack) {
  if (sodium_init() < 0) {
    ESP_LOGE(TAG, "libsodium init failed");
    return -1;
  }
  memcpy(s_pubkey, server_pubkey, sizeof s_pubkey);
  s_ack = ack;
  memset(s_seen, 0, sizeof s_seen);
  s_seen_head = 0;
  return 0;
}

void commands_set_pubkey(const uint8_t server_pubkey[32]) {
  memcpy(s_pubkey, server_pubkey, sizeof s_pubkey);
}

static bool already_seen(const char *id) {
  for (int i = 0; i < CMD_DEDUPE_N; i++) {
    if (strncmp(s_seen[i], id, CMD_ID_MAX) == 0) {
      return true;
    }
  }
  return false;
}

static void remember(const char *id) {
  snprintf(s_seen[s_seen_head], CMD_ID_MAX, "%s", id);
  s_seen_head = (s_seen_head + 1) % CMD_DEDUPE_N;
}

static void ack(const char *command_id) {
  if (s_ack != NULL) {
    s_ack((const uint8_t *)command_id, strlen(command_id));
  }
}

// Dispatch a verified, deduped command to the executor. Returns true if the
// command was accepted (enqueued -> ack); false if the executor queue was full
// (no ack -> server re-issues). BAD_PARAMS/UNKNOWN_TYPE are enqueued (and acked)
// but the executor task does no work — see spec §6.1/§6.2.
static bool execute(const char *type, const cJSON *root) {
  const cJSON *params = cJSON_GetObjectItemCaseSensitive(root, "params");
  return executor_submit(type, params);
}

void commands_handle(const uint8_t *data, size_t len) {
  if (len <= ED25519_SIG_LEN) {
    ESP_LOGW(TAG, "command too short (%u bytes)", (unsigned)len);
    return;
  }
  const uint8_t *sig = data;
  const uint8_t *payload = data + ED25519_SIG_LEN;
  size_t payload_len = len - ED25519_SIG_LEN;

  if (crypto_sign_ed25519_verify_detached(sig, payload, payload_len, s_pubkey) != 0) {
    ESP_LOGW(TAG, "command signature INVALID — dropped");  // forged/tampered by relay
    return;
  }

  cJSON *root = cJSON_ParseWithLength((const char *)payload, payload_len);
  if (root == NULL) {
    ESP_LOGW(TAG, "command payload not valid JSON — dropped");
    return;
  }
  const cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "command_id");
  const cJSON *type = cJSON_GetObjectItemCaseSensitive(root, "type");
  if (!cJSON_IsString(id) || !cJSON_IsString(type)) {
    ESP_LOGW(TAG, "command missing command_id/type — dropped");
    cJSON_Delete(root);
    return;
  }

  if (already_seen(id->valuestring)) {
    ack(id->valuestring);  // at-least-once: re-ack, but execute only once
  } else {
    remember(id->valuestring);
    if (execute(type->valuestring, root)) {
      ack(id->valuestring);   // accepted -> ack (at-least-once)
    } else {
      ESP_LOGW(TAG, "executor queue full — no ack for %s", id->valuestring);
    }
  }
  cJSON_Delete(root);
}
