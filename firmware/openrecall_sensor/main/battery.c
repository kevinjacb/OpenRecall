/*
 * Battery monitor implementation — D0/GPIO1 ADC1 voltage divider (spec 3.5).
 *
 * Ported from the selftest test_battery.c. ADC1_CH0 oneshot + curve-fitting
 * cali, 64-sample oversample (drop min/max), x2.0 divider. A low-priority task
 * samples every BATTERY_SAMPLE_INTERVAL_S and caches the result.
 */
#include "battery.h"
#include "ble_link.h"
#include "config.h"

#include <stdbool.h>
#include <stdio.h>

#include "esp_log.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "battery";

static adc_oneshot_unit_handle_t s_adc = NULL;
static adc_cali_handle_t s_cali = NULL;
static volatile int s_latest_mv = 0;   /* last sampled Vbat mV (0 = never) */

esp_err_t battery_init(void) {
  adc_oneshot_unit_init_cfg_t init_cfg = { .unit_id = ADC_UNIT_1 };
  esp_err_t err = adc_oneshot_new_unit(&init_cfg, &s_adc);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "adc init: %s", esp_err_to_name(err));
    return err;
  }

  adc_oneshot_chan_cfg_t chan_cfg = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12 };
  err = adc_oneshot_config_channel(s_adc, ADC_CHANNEL_0, &chan_cfg);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "adc cfg: %s", esp_err_to_name(err));
    return err;
  }

  /* Curve-fitting calibration converts raw ADC counts -> mV at the pin
   * (accounts for the ADC's non-linear atten curve). Supported on the S3. */
  adc_cali_curve_fitting_config_t cali_cfg = {
    .unit_id = ADC_UNIT_1, .chan = ADC_CHANNEL_0,
    .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12 };
  err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &s_cali);
  if (err != ESP_OK) {
    ESP_LOGW(TAG, "cali unavailable (%s) — using raw counts", esp_err_to_name(err));
    s_cali = NULL;   /* read_mv falls back to a raw->mV estimate */
  }
  ESP_LOGI(TAG, "ADC1_CH0/GPIO%d ready (x%.1f divider)", VBAT_GPIO, (double)VBAT_DIVIDER);
  return ESP_OK;
}

int battery_read_mv(void) {
  if (s_adc == NULL) return 0;

  int raw[BATTERY_OVERSAMPLE];
  for (int i = 0; i < BATTERY_OVERSAMPLE; i++) {
    int v = 0;
    if (adc_oneshot_read(s_adc, ADC_CHANNEL_0, &v) != ESP_OK) v = 0;
    raw[i] = v;
    vTaskDelay(pdMS_TO_TICKS(1));   /* spread samples; ~64 ms total */
  }
  int mn = raw[0], mx = raw[0];
  long sum = 0;
  for (int i = 0; i < BATTERY_OVERSAMPLE; i++) {
    if (raw[i] < mn) mn = raw[i];
    if (raw[i] > mx) mx = raw[i];
    sum += raw[i];
  }
  long avg = (sum - mn - mx) / (BATTERY_OVERSAMPLE - 2);

  int vadc_mv = 0;
  if (s_cali != NULL) {
    adc_cali_raw_to_voltage(s_cali, (int)avg, &vadc_mv);
  } else {
    /* No cali: raw 12-bit @ 12 dB atten -> ~0..2500 mV full-scale estimate.
     * Coarse, but better than nothing if cali init failed. */
    vadc_mv = (int)((avg * 2500L) / 4095L);
  }
  int vbat_mv = (int)(vadc_mv * VBAT_DIVIDER + 0.5f);
  return vbat_mv;
}

uint8_t battery_percent(int vbat_mv) {
  if (vbat_mv <= VBAT_MIN_MV) return 0;
  if (vbat_mv >= VBAT_FULL_MV) return 100;
  /* linear between floor and full; clamp to 0..100 */
  int pct = (vbat_mv - VBAT_MIN_MV) * 100 / (VBAT_FULL_MV - VBAT_MIN_MV);
  if (pct < 0) pct = 0;
  if (pct > 100) pct = 100;
  return (uint8_t)pct;
}

/* True until the first telemetry after a button (ext0) deep-sleep wake has
 * been sent; that frame carries "wake_reason":"button" so the server clears
 * desired sleep mode (spec D2). Set from app_main, consumed once here. */
static volatile bool s_wake_from_button = false;

void battery_mark_wake_from_button(void) { s_wake_from_button = true; }

/* Push one §E-shaped telemetry JSON over the ACK notify characteristic.
 * The relay recognises a payload starting with '{' as device control JSON
 * (a plain command-id ack never starts with '{'), injects the session id,
 * and forwards it as the server's `telemetry` frame — which feeds the
 * ReportedCapabilityProvider and the app's /device/status battery tile.
 * notify() silently drops when the phone isn't subscribed; that's fine,
 * the next sample retries. */
static void telemetry_notify(int mv, const char *state) {
  char pct_str[8];
  unsigned pct = battery_percent(mv);
  if (pct >= 100) {
    snprintf(pct_str, sizeof pct_str, "1.00");
  } else {
    snprintf(pct_str, sizeof pct_str, "0.%02u", pct);
  }
  char buf[128];
  int n;
  if (s_wake_from_button) {
    n = snprintf(buf, sizeof buf,
                 "{\"type\":\"telemetry\",\"battery_pct\":%s,\"state\":\"%s\","
                 "\"wake_reason\":\"button\"}", pct_str, state);
  } else {
    n = snprintf(buf, sizeof buf,
                 "{\"type\":\"telemetry\",\"battery_pct\":%s,\"state\":\"%s\"}",
                 pct_str, state);
  }
  if (n > 0 && n < (int)sizeof buf) {
    if (ble_link_notify_ack((const uint8_t *)buf, (size_t)n) == 0) {
      /* Delivered: the wake reason has been reported; do not repeat it. */
      s_wake_from_button = false;
    }
  }
}

void battery_notify_sleeping(void) {
  int mv = s_latest_mv;
  if (mv <= 0) return;  /* no sample yet — a stale 0% would mislead */
  telemetry_notify(mv, "sleeping");
}

static void battery_monitor_task(void *arg) {
  (void)arg;
  for (;;) {
    int mv = battery_read_mv();
    if (mv > 0) {
      s_latest_mv = mv;
      ESP_LOGI(TAG, "Vbat=%dmV (%d%%)", mv, battery_percent(mv));
      telemetry_notify(mv, "active");
    }
    vTaskDelay(pdMS_TO_TICKS(BATTERY_SAMPLE_INTERVAL_S * 1000));
  }
}

void battery_start_monitor(void) {
  /* Low priority: battery sampling must never contend with audio (core 1)
   * or BLE (core 0) real-time. Pin to core 0 with the radio stack. 4 KB is
   * plenty for the oversample loop + log. */
  xTaskCreatePinnedToCore(battery_monitor_task, "battery", 4096, NULL, 1, NULL, 0);
}

int battery_latest_mv(void) {
  return s_latest_mv;   /* 32-bit aligned volatile read — atomic on Xtensa */
}