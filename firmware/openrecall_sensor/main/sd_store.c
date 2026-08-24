/*
 * P4b SD store — microSD over SPI (sdspi + FatFS) on the XIAO ESP32S3 Sense
 * reserved SD pins (see config.h: SD_PIN_SCK/MISO/MOSI/CS @ SD_SPI_HZ).
 *
 * Lazy mount: sd_store_mount() is idempotent (static s_mounted flag). After
 * mount the VFS registers SD_MOUNT_POINT so standard C fopen/fwrite/fread/
 * fclose/unlink work on "/sdcard/..." paths. The snapshot + video dirs are
 * created on first mount (mkdir, "already exists" ignored).
 *
 * Pattern follows the ESP-IDF 5.1.6 sdspi example:
 *   spi_bus_initialize(host.slot, &bus_cfg, SDSPI_DEFAULT_DMA)
 *   -> esp_vfs_fat_sdspi_mount(mount_point, &host, &slot, &mount_config, &card)
 */
#include "sd_store.h"
#include "config.h"

#include "esp_log.h"
#include "esp_vfs_fat.h"
#include "sdmmc_cmd.h"
#include "driver/sdspi_host.h"
#include "driver/spi_master.h"

#include <errno.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/unistd.h>

static const char *TAG = "sd_store";

static bool s_mounted = false;
static sdmmc_card_t *s_card = NULL;

/* One-shot mkdir that treats "already exists" as success. */
static esp_err_t sd_ensure_dir(const char *path) {
  if (mkdir(path, 0777) == 0) return ESP_OK;
  if (errno == EEXIST) return ESP_OK;
  ESP_LOGE(TAG, "mkdir(%s): %s", path, strerror(errno));
  return ESP_FAIL;
}

esp_err_t sd_store_mount(void) {
  if (s_mounted) return ESP_OK;

  sdmmc_host_t host = SDSPI_HOST_DEFAULT();
  host.max_freq_khz = SD_SPI_HZ / 1000;        /* 20000 kHz = 20 MHz */

  spi_bus_config_t bus_cfg = {
    .mosi_io_num = SD_PIN_MOSI,
    .miso_io_num = SD_PIN_MISO,
    .sclk_io_num = SD_PIN_SCK,
    .quadwp_io_num = -1,
    .quadhd_io_num = -1,
    .max_transfer_sz = 4000,
  };
  esp_err_t err = spi_bus_initialize(host.slot, &bus_cfg, SDSPI_DEFAULT_DMA);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "spi_bus_initialize: %s", esp_err_to_name(err));
    return err;
  }

  sdspi_device_config_t slot_config = SDSPI_DEVICE_CONFIG_DEFAULT();
  slot_config.gpio_cs = SD_PIN_CS;
  slot_config.host_id = host.slot;

  esp_vfs_fat_sdmmc_mount_config_t mount_config = {
    .format_if_mount_failed = false,
    .max_files = 5,
    .allocation_unit_size = 16 * 1024,
  };
  err = esp_vfs_fat_sdspi_mount(SD_MOUNT_POINT, &host, &slot_config,
                                &mount_config, &s_card);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "esp_vfs_fat_sdspi_mount: %s", esp_err_to_name(err));
    spi_bus_free(host.slot);
    return err;
  }

  /* Create top-level capture dirs; per-boot subdirs are the caller's job.
   * sd_ensure_dir logs + treats EEXIST as success, so a non-fatal mkdir error
   * here does not abort the mount. */
  (void)sd_ensure_dir(SD_SNAPSHOT_DIR);
  (void)sd_ensure_dir(SD_VIDEO_DIR);

  s_mounted = true;
  ESP_LOGI(TAG, "mounted %s (sdspi @%ld kHz)", SD_MOUNT_POINT,
           (long)SD_SPI_HZ / 1000);
  return ESP_OK;
}

esp_err_t sd_store_write(const char *path, const uint8_t *data, size_t len) {
  FILE *f = fopen(path, "wb");
  if (f == NULL) {
    ESP_LOGE(TAG, "fopen(%s, wb): %s", path, strerror(errno));
    return ESP_FAIL;
  }
  size_t n = fwrite(data, 1, len, f);
  fclose(f);
  if (n != len) {
    ESP_LOGE(TAG, "short write %s: %zu/%zu", path, n, len);
    return ESP_FAIL;
  }
  return ESP_OK;
}

esp_err_t sd_store_append(const char *path, const char *line) {
  FILE *f = fopen(path, "a");
  if (f == NULL) {
    ESP_LOGE(TAG, "fopen(%s, a): %s", path, strerror(errno));
    return ESP_FAIL;
  }
  int rc = fputs(line, f);
  fclose(f);
  if (rc == EOF) {
    ESP_LOGE(TAG, "fputs(%s): %s", path, strerror(errno));
    return ESP_FAIL;
  }
  return ESP_OK;
}

esp_err_t sd_store_read(const char *path, uint8_t *buf, size_t cap,
                        size_t *out_len) {
  if (out_len) *out_len = 0;
  FILE *f = fopen(path, "rb");
  if (f == NULL) {
    ESP_LOGE(TAG, "fopen(%s, rb): %s", path, strerror(errno));
    return ESP_FAIL;
  }
  size_t n = fread(buf, 1, cap, f);
  fclose(f);
  if (out_len) *out_len = n;
  return ESP_OK;
}

esp_err_t sd_store_read_manifest(char *buf, size_t cap, size_t *out_len) {
  if (out_len) *out_len = 0;
  FILE *f = fopen(SD_MANIFEST, "rb");
  if (f == NULL) {
    /* Missing manifest is not an error — empty manifest. */
    return ESP_OK;
  }
  size_t n = fread(buf, 1, cap, f);
  fclose(f);
  if (out_len) *out_len = n;
  return ESP_OK;
}

esp_err_t sd_store_delete(const char *path) {
  if (unlink(path) == 0) return ESP_OK;
  if (errno == ENOENT) return ESP_OK;   /* idempotent */
  ESP_LOGE(TAG, "unlink(%s): %s", path, strerror(errno));
  return ESP_FAIL;
}