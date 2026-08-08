/* firmware/openrecall_sensor/main/provisioning.c
 *
 * ESP-IDF glue: NVS-backed prov_store_t + NimBLE GATT access callbacks for the
 * provisioning service. The state machine + NVS-write-first ordering live in
 * provisioning_core.c (Task 6, host-tested); this file just wires that core to
 * real NVS and to the GATT access callbacks registered in ble_link.c.
 */
#include "provisioning.h"
#include "provisioning_core.h"
#include "commands.h"
#include "config.h"
#include <string.h>
#include "nvs_flash.h"
#include "esp_log.h"
#include "host/ble_hs.h"
#include "host/ble_gatt.h"

static const char *TAG = "provisioning";

/* --- NVS-backed prov_store_t --- */

static int nvs_get(uint8_t out_key[32], uint8_t *out_provisioned) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READONLY, &h) != ESP_OK) return 1;
  uint8_t prov = 0;
  size_t len = 32;
  esp_err_t e = nvs_get_blob(h, PROV_NVS_KEY, out_key, &len);
  if (e != ESP_OK || len != 32) { nvs_close(h); return 1; }
  nvs_get_u8(h, PROV_NVS_PROVISIONED, &prov);
  nvs_close(h);
  *out_provisioned = prov;
  return 0;
}

static int nvs_set(const uint8_t key[32]) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return -1;
  if (nvs_set_blob(h, PROV_NVS_KEY, key, 32) != ESP_OK) { nvs_close(h); return -1; }
  nvs_set_u8(h, PROV_NVS_PROVISIONED, 1);   /* best-effort flag */
  if (nvs_commit(h) != ESP_OK) { nvs_close(h); return -1; }
  nvs_close(h);
  return 0;
}

static int nvs_clear(void) {
  nvs_handle_t h;
  if (nvs_open(PROV_NVS_NAMESPACE, NVS_READWRITE, &h) != ESP_OK) return -1;
  esp_err_t e = nvs_erase_key(h, PROV_NVS_KEY);
  if (e != ESP_OK && e != ESP_ERR_NVS_NOT_FOUND) { nvs_close(h); return -1; }
  nvs_set_u8(h, PROV_NVS_PROVISIONED, 0);
  if (nvs_commit(h) != ESP_OK) { nvs_close(h); return -1; }
  nvs_close(h);
  return 0;
}

static const prov_store_t s_store = { nvs_get, nvs_set, nvs_clear };

int provisioning_init(void) {
  if (provisioning_core_init(&s_store) != 0) {
    ESP_LOGE(TAG, "core init failed");
    return -1;
  }
  if (provisioning_core_state() == PROV_PROVISIONED) {
    commands_set_pubkey(provisioning_core_server_key());
    ESP_LOGI(TAG, "booted provisioned");
  } else {
    ESP_LOGI(TAG, "booted unprovisioned");
  }
  return 0;
}

uint8_t provisioning_state_byte(void) {
  return (uint8_t)provisioning_core_state();
}

/* --- GATT access callbacks ---
 *
 * Write handlers mirror ble_link.c's command_write_access: OS_MBUF_PKTLEN for the
 * length and ble_hs_mbuf_to_flat to flatten the mbuf. The STATE notify is sent via
 * provisioning_notify_state(), which is implemented in ble_link.c and uses the same
 * notify() helper (ble_hs_mbuf_from_flat + ble_gatts_notify_custom) as the AUDIO/ACK
 * characteristics. */

int prov_state_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                      void *arg) {
  (void)conn; (void)attr; (void)arg;
  if (ctxt->op == BLE_GATT_ACCESS_OP_READ_CHR) {
    uint8_t v = provisioning_state_byte();
    os_mbuf_append(ctxt->om, &v, 1);  /* matches ble_link.c chr read style */
    return 0;
  }
  return 0;  /* writes not supported on STATE */
}

int prov_key_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                    void *arg) {
  (void)conn; (void)attr; (void)arg;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return 0;
  uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
  if (len != 32) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  uint8_t key[32];
  if (ble_hs_mbuf_to_flat(ctxt->om, key, sizeof key, NULL) != 0) return BLE_ATT_ERR_UNLIKELY;
  /* provisioning_core_apply_key writes NVS first (via the store above) then flips
   * state, so a reboot sees the persisted key. Reject if already provisioned. */
  if (provisioning_core_apply_key(key) != 0) {
    ESP_LOGW(TAG, "key write rejected (already provisioned)");
    return BLE_ATT_ERR_INSUFFICIENT_AUTHEN;
  }
  commands_set_pubkey(provisioning_core_server_key());
  provisioning_notify_state();
  return 0;
}

int prov_reset_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                      void *arg) {
  (void)conn; (void)attr; (void)arg;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) return 0;
  uint32_t magic = 0;
  uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
  if (len != sizeof magic) return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  if (ble_hs_mbuf_to_flat(ctxt->om, &magic, sizeof magic, NULL) != 0) return BLE_ATT_ERR_UNLIKELY;
  if (magic != PROV_FACTORY_RESET_MAGIC) return BLE_ATT_ERR_UNLIKELY;
  if (provisioning_core_factory_reset() != 0) return BLE_ATT_ERR_UNLIKELY;
  uint8_t zero[32] = {0};
  commands_set_pubkey(zero);
  provisioning_notify_state();
  return 0;
}