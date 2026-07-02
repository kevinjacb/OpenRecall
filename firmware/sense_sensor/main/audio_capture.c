#include "audio_capture.h"

#include "driver/i2s_pdm.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio";

static i2s_chan_handle_t s_rx_chan;

esp_err_t audio_capture_init(void) {
  // One RX channel; DMA buffers sized to a frame so each read returns ~20 ms.
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = 6;
  chan_cfg.dma_frame_num = FRAME_SAMPLES;
  ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan), TAG, "new channel");

  i2s_pdm_rx_config_t pdm_cfg = {
      .clk_cfg = I2S_PDM_RX_CLK_DEFAULT_CONFIG(SAMPLE_RATE),
      .slot_cfg = I2S_PDM_RX_SLOT_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_MONO),
      .gpio_cfg = {
          .clk = PDM_CLK_GPIO,
          .din = PDM_DIN_GPIO,
          .invert_flags = {.clk_inv = false},
      },
  };
  ESP_RETURN_ON_ERROR(i2s_channel_init_pdm_rx_mode(s_rx_chan, &pdm_cfg), TAG, "init pdm rx");
  ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_chan), TAG, "enable");
  ESP_LOGI(TAG, "PDM RX up: %d Hz mono, clk=%d din=%d", SAMPLE_RATE, PDM_CLK_GPIO, PDM_DIN_GPIO);
  return ESP_OK;
}

esp_err_t audio_capture_read_frame(int16_t *out) {
  size_t bytes_read = 0;
  esp_err_t err = i2s_channel_read(s_rx_chan, out, FRAME_BYTES, &bytes_read, portMAX_DELAY);
  if (err == ESP_OK && bytes_read != FRAME_BYTES) {
    return ESP_ERR_INVALID_SIZE;
  }
  return err;
}
