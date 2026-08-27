/*
 * Battery test — voltage divider on D1 (GPIO2 = ADC1 channel 1).
 *
 * 100k/100k divider: Vbat = Vadc * 2.0. 11 dB attenuation so a full 4.2 V LiPo
 * (-> 2.1 V at the pin) fits the ADC range. Uses the IDF-5.1 oneshot ADC +
 * curve-fitting calibration (single `esp_adc` component; supported on the S3).
 * Oversamples 64 readings, drops the min/max, averages the rest for stability.
 */
#include "selftest.h"
#include "selftest_config.h"
#include "esp_log.h"
#include "esp_err.h"
#include "esp_adc/adc_oneshot.h"
#include "esp_adc/adc_cali.h"
#include "esp_adc/adc_cali_scheme.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include <stdio.h>

static const char *TAG = "battery";

#define BAT_SAMPLES 64

selftest_result_t test_battery(void) {
  selftest_result_t r = {0};
  ESP_LOGI(TAG, "init ADC1 channel 0 (GPIO%d, 12 dB atten)...", VBAT_GPIO);

  adc_oneshot_unit_handle_t adc = NULL;
  adc_oneshot_unit_init_cfg_t init_cfg = { .unit_id = ADC_UNIT_1 };
  esp_err_t err = adc_oneshot_new_unit(&init_cfg, &adc);
  if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "adc init: %s", esp_err_to_name(err)); return r; }

  adc_oneshot_chan_cfg_t chan_cfg = { .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12 };
  err = adc_oneshot_config_channel(adc, ADC_CHANNEL_0, &chan_cfg);
  if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "adc cfg: %s", esp_err_to_name(err)); return r; }

  adc_cali_handle_t cali = NULL;
  adc_cali_curve_fitting_config_t cali_cfg = {
    .unit_id = ADC_UNIT_1, .chan = ADC_CHANNEL_0, .atten = ADC_ATTEN_DB_12, .bitwidth = ADC_BITWIDTH_12 };
  err = adc_cali_create_scheme_curve_fitting(&cali_cfg, &cali);
  if (err != ESP_OK) { r.status = ST_FAIL; snprintf(r.detail, sizeof r.detail, "cali: %s", esp_err_to_name(err)); return r; }

  int raw[BAT_SAMPLES];
  for (int i = 0; i < BAT_SAMPLES; i++) {
    int v = 0;
    if (adc_oneshot_read(adc, ADC_CHANNEL_0, &v) != ESP_OK) v = 0;
    raw[i] = v;
    vTaskDelay(pdMS_TO_TICKS(2));
  }
  int mn = raw[0], mx = raw[0];
  long sum = 0;
  for (int i = 0; i < BAT_SAMPLES; i++) {
    if (raw[i] < mn) mn = raw[i];
    if (raw[i] > mx) mx = raw[i];
    sum += raw[i];
  }
  long avg = (sum - mn - mx) / (BAT_SAMPLES - 2);

  int vadc_mv = 0;
  adc_cali_raw_to_voltage(cali, (int)avg, &vadc_mv);
  int vbat_mv = (int)(vadc_mv * VBAT_DIVIDER + 0.5f);

  ESP_LOGI(TAG, "raw_avg=%ld vadc=%dmV vbat=%dmV", avg, vadc_mv, vbat_mv);

  if (vbat_mv >= VBAT_MIN_MV && vbat_mv <= VBAT_MAX_MV) {
    r.status = ST_PASS;
    snprintf(r.detail, sizeof r.detail, "Vbat=%.2fV", vbat_mv / 1000.0f);
  } else if (vbat_mv < 200) {
    r.status = ST_FAIL;
    snprintf(r.detail, sizeof r.detail, "Vbat=%.2fV (divider not connected?)", vbat_mv / 1000.0f);
  } else {
    r.status = ST_FAIL;
    snprintf(r.detail, sizeof r.detail, "Vbat=%.2fV out of range", vbat_mv / 1000.0f);
  }
  return r;
}