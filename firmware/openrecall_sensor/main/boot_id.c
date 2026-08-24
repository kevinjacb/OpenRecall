#include "boot_id.h"
#include "config.h"
#include "nvs_flash.h"
#include "esp_log.h"

#include <inttypes.h>  /* PRIu32 */

static const char *TAG = "boot_id";
static uint32_t s_boot_id = 0;

esp_err_t boot_id_init(void) {
  nvs_handle_t h;
  esp_err_t err = nvs_open(BOOT_NVS_NAMESPACE, NVS_READWRITE, &h);
  if (err != ESP_OK) { ESP_LOGE(TAG, "nvs_open: %s", esp_err_to_name(err)); return err; }
  uint32_t id = 0;
  err = nvs_get_u32(h, BOOT_NVS_KEY, &id);   /* ESP_ERR_NVS_NOT_FOUND -> id stays 0 */
  if (err != ESP_OK && err != ESP_ERR_NVS_NOT_FOUND) {
    ESP_LOGW(TAG, "nvs_get: %s (starting from 0)", esp_err_to_name(err));
    id = 0;
  }
  id += 1;                                   /* increment each activate */
  err = nvs_set_u32(h, BOOT_NVS_KEY, id);
  if (err != ESP_OK) { ESP_LOGE(TAG, "nvs_set: %s", esp_err_to_name(err)); nvs_close(h); return err; }
  nvs_commit(h);
  nvs_close(h);
  s_boot_id = id;
  ESP_LOGI(TAG, "boot_id=%" PRIu32, (uint32_t)s_boot_id);
  return ESP_OK;
}

uint32_t boot_id_get(void) { return s_boot_id; }