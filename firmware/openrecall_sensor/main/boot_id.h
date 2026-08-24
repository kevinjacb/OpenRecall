/*
 * P4b boot_id — a per-activate counter in NVS. rel_ts_ms resets to 0 at every
 * boot, so two captures with rel_ts_ms=5000 from different boots would collide
 * on the server's session timeline; boot_id disambiguates them. Incremented
 * once per activate (boot_id_init), cached in RAM, exposed via boot_id_get.
 * Also used in the SoftAP SSID ("OpenRecall-<boot_id>").
 */
#pragma once
#include "esp_err.h"
#include <stdint.h>

esp_err_t boot_id_init(void);   /* call once from app_main after nvs_flash_init */
uint32_t boot_id_get(void);     /* the current boot's id (0 on init failure) */