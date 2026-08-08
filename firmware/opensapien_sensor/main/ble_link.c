#include "ble_link.h"

#include "config.h"
#include "provisioning.h"
#include "esp_log.h"
#include "host/ble_hs.h"
#include "host/ble_att.h"
#include "host/util/util.h"
#include "nimble/nimble_port.h"
#include "nimble/nimble_port_freertos.h"
#include "services/gap/ble_svc_gap.h"
#include "services/gatt/ble_svc_gatt.h"

#include <inttypes.h>  // PRIu16 — for uint16_t (BLE connection handle)

static const char *TAG = "ble";

#define BLE_CMD_MAX 512  // a signed §D command envelope is well under this

// 128-bit UUIDs in NimBLE little-endian byte order (reverse of the textual UUID).
// Base 6e9d000X-b5a3-4f6e-9b1a-7c2d5e8f0a10; byte at index 12 selects the char.
#define OPENSAPIEN_UUID128(variant)                                                        \
  BLE_UUID128_INIT(0x10, 0x0a, 0x8f, 0x5e, 0x2d, 0x7c, 0x1a, 0x9b, 0x6e, 0x4f, 0xa3, \
                   0xb5, (variant), 0x00, 0x9d, 0x6e)

static const ble_uuid128_t s_svc_uuid = OPENSAPIEN_UUID128(0x01);
static const ble_uuid128_t s_audio_uuid = OPENSAPIEN_UUID128(0x02);
static const ble_uuid128_t s_command_uuid = OPENSAPIEN_UUID128(0x03);
static const ble_uuid128_t s_ack_uuid = OPENSAPIEN_UUID128(0x04);

// Provisioning service (UUID 0x10) + its three characteristics (0x11..0x13).
static const ble_uuid128_t s_prov_svc_uuid   = OPENSAPIEN_UUID128(0x10);
static const ble_uuid128_t s_prov_state_uuid = OPENSAPIEN_UUID128(0x11);
static const ble_uuid128_t s_prov_key_uuid   = OPENSAPIEN_UUID128(0x12);
static const ble_uuid128_t s_prov_reset_uuid = OPENSAPIEN_UUID128(0x13);

static uint16_t s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
static uint16_t s_audio_handle;  // value handle for the audio notify char
static uint16_t s_ack_handle;    // value handle for the ack notify char
static uint16_t s_prov_state_handle;  // value handle for the provisioning STATE char
static bool s_audio_subscribed;
static bool s_ack_subscribed;
static bool s_prov_state_subscribed;
static uint8_t s_addr_type;
static ble_command_handler_t s_on_command;

static void start_advertising(void);

// Notify chars are write-from-server-only; reads return empty.
static int chr_noop_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                           void *arg) {
  (void)conn; (void)attr; (void)ctxt; (void)arg;
  return 0;
}

// The phone writes a signed §D command here; hand it to the command module.
static int command_write_access(uint16_t conn, uint16_t attr, struct ble_gatt_access_ctxt *ctxt,
                                 void *arg) {
  (void)conn; (void)attr; (void)arg;
  if (ctxt->op != BLE_GATT_ACCESS_OP_WRITE_CHR) {
    return BLE_ATT_ERR_UNLIKELY;
  }
  uint16_t len = OS_MBUF_PKTLEN(ctxt->om);
  if (len == 0 || len > BLE_CMD_MAX) {
    return BLE_ATT_ERR_INVALID_ATTR_VALUE_LEN;
  }
  static uint8_t buf[BLE_CMD_MAX];
  uint16_t copied = 0;
  if (ble_hs_mbuf_to_flat(ctxt->om, buf, sizeof buf, &copied) != 0) {
    return BLE_ATT_ERR_UNLIKELY;
  }
  if (s_on_command != NULL) {
    s_on_command(buf, copied);
  }
  return 0;
}

static const struct ble_gatt_svc_def s_gatt_svcs[] = {
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &s_audio_uuid.u,
                .access_cb = chr_noop_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_audio_handle,
            },
            {
                .uuid = &s_command_uuid.u,
                .access_cb = command_write_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &s_ack_uuid.u,
                .access_cb = chr_noop_access,
                .flags = BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_ack_handle,
            },
            {0},
        },
    },
    {
        .type = BLE_GATT_SVC_TYPE_PRIMARY,
        .uuid = &s_prov_svc_uuid.u,
        .characteristics = (struct ble_gatt_chr_def[]){
            {
                .uuid = &s_prov_state_uuid.u,
                .access_cb = prov_state_access,
                .flags = BLE_GATT_CHR_F_READ | BLE_GATT_CHR_F_NOTIFY,
                .val_handle = &s_prov_state_handle,
            },
            {
                .uuid = &s_prov_key_uuid.u,
                .access_cb = prov_key_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {
                .uuid = &s_prov_reset_uuid.u,
                .access_cb = prov_reset_access,
                .flags = BLE_GATT_CHR_F_WRITE,
            },
            {0},
        },
    },
    {0},
};

static int gap_event(struct ble_gap_event *event, void *arg) {
  (void)arg;
  switch (event->type) {
    case BLE_GAP_EVENT_CONNECT:
      if (event->connect.status == 0) {
        s_conn_handle = event->connect.conn_handle;
        ESP_LOGI(TAG, "phone connected (handle %" PRIu16 ")", s_conn_handle);
      } else {
        start_advertising();  // failed; advertise again
      }
      return 0;
    case BLE_GAP_EVENT_DISCONNECT:
      ESP_LOGI(TAG, "phone disconnected (reason %d)", event->disconnect.reason);
      s_conn_handle = BLE_HS_CONN_HANDLE_NONE;
      s_audio_subscribed = false;
      s_ack_subscribed = false;
      s_prov_state_subscribed = false;
      start_advertising();
      return 0;
    case BLE_GAP_EVENT_SUBSCRIBE:
      if (event->subscribe.attr_handle == s_audio_handle) {
        s_audio_subscribed = event->subscribe.cur_notify;
      } else if (event->subscribe.attr_handle == s_ack_handle) {
        s_ack_subscribed = event->subscribe.cur_notify;
      } else if (event->subscribe.attr_handle == s_prov_state_handle) {
        s_prov_state_subscribed = event->subscribe.cur_notify;
      }
      return 0;
    default:
      return 0;
  }
}

static void start_advertising(void) {
  struct ble_hs_adv_fields fields = {0};
  fields.flags = BLE_HS_ADV_F_DISC_GEN | BLE_HS_ADV_F_BREDR_UNSUP;
  fields.name = (uint8_t *)"OpenSapien";
  fields.name_len = 5;
  fields.name_is_complete = 1;
  fields.uuids128 = (ble_uuid128_t *)&s_svc_uuid;
  fields.num_uuids128 = 1;
  fields.uuids128_is_complete = 1;
  int rc = ble_gap_adv_set_fields(&fields);
  if (rc != 0) {
    ESP_LOGE(TAG, "adv_set_fields rc=%d", rc);
    return;
  }
  struct ble_gap_adv_params adv_params = {0};
  adv_params.conn_mode = BLE_GAP_CONN_MODE_UND;
  adv_params.disc_mode = BLE_GAP_DISC_MODE_GEN;
  rc = ble_gap_adv_start(s_addr_type, NULL, BLE_HS_FOREVER, &adv_params, gap_event, NULL);
  if (rc != 0) {
    ESP_LOGE(TAG, "adv_start rc=%d", rc);
  } else {
    ESP_LOGI(TAG, "advertising as \"OpenSapien\"");
  }
}

static void on_sync(void) {
  ble_hs_id_infer_auto(0, &s_addr_type);
  start_advertising();
}

static void ble_host_task(void *param) {
  (void)param;
  nimble_port_run();  // returns only on nimble_port_stop()
  nimble_port_freertos_deinit();
}

static int notify(uint16_t attr_handle, bool subscribed, const uint8_t *data, size_t len) {
  if (s_conn_handle == BLE_HS_CONN_HANDLE_NONE || !subscribed) {
    return -1;
  }
  struct os_mbuf *om = ble_hs_mbuf_from_flat(data, len);
  if (om == NULL) {
    return -1;
  }
  return ble_gatts_notify_custom(s_conn_handle, attr_handle, om);
}

int ble_link_notify_audio(const uint8_t *data, size_t len) {
  return notify(s_audio_handle, s_audio_subscribed, data, len);
}

int ble_link_notify_ack(const uint8_t *data, size_t len) {
  return notify(s_ack_handle, s_ack_subscribed, data, len);
}

/* Push the current provisioning state (1 byte) to subscribed STATE clients.
 * Used by provisioning.c after a key write or factory reset. */
void provisioning_notify_state(void) {
  uint8_t v = provisioning_state_byte();
  notify(s_prov_state_handle, s_prov_state_subscribed, &v, 1);
}

bool ble_link_connected(void) {
  return s_conn_handle != BLE_HS_CONN_HANDLE_NONE;
}

uint16_t ble_link_att_mtu(void) {
  // ble_att_mtu returns the negotiated ATT MTU for the connection, or 0 if the
  // connection doesn't exist / MTU hasn't been exchanged. Guard explicitly so
  // we never pass BLE_HS_CONN_HANDLE_NONE into it.
  if (!ble_link_connected()) {
    return 0;
  }
  return ble_att_mtu(s_conn_handle);
}

esp_err_t ble_link_start(ble_command_handler_t on_command) {
  s_on_command = on_command;

  esp_err_t err = nimble_port_init();
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "nimble_port_init failed: %d", err);
    return err;
  }

  ble_svc_gap_init();
  ble_svc_gatt_init();

  int rc = ble_gatts_count_cfg(s_gatt_svcs);
  if (rc != 0) return ESP_FAIL;
  rc = ble_gatts_add_svcs(s_gatt_svcs);
  if (rc != 0) return ESP_FAIL;
  if (ble_svc_gap_device_name_set("OpenSapien") != 0) return ESP_FAIL;

  ble_hs_cfg.sync_cb = on_sync;

  nimble_port_freertos_init(ble_host_task);
  ESP_LOGI(TAG, "NimBLE started");
  return ESP_OK;
}
