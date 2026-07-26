#include "audio_capture.h"

#include "driver/i2s_std.h"
#include "esp_check.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "audio";

static i2s_chan_handle_t s_rx_chan;

esp_err_t audio_capture_init(void) {
  // One RX channel; DMA buffers sized to a frame so each read returns ~20 ms.
  i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
  chan_cfg.dma_desc_num = 6;
  chan_cfg.dma_frame_num = FRAME_SAMPLES;   // 320 frames per channel per DMA desc
  ESP_RETURN_ON_ERROR(i2s_new_channel(&chan_cfg, NULL, &s_rx_chan), TAG, "new channel");

  i2s_std_clk_config_t clk_cfg = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE);
  i2s_std_gpio_config_t gpio_cfg = {
      .mclk = -1,                 // MCLK not used by the IENMP441 (NC)
      .bclk = I2S_BCK_GPIO,
      .ws   = I2S_WS_GPIO,
      .dout = -1,                 // RX only
      .din  = I2S_DATA_GPIO,
      .invert_flags = {.mclk_inv = false, .bclk_inv = false, .ws_inv = false},
  };
  // Philips stereo, 16-bit, both slots (L+R). I2S_SLOT_MODE_STEREO selects
  // I2S_STD_SLOT_BOTH inside the macro so both mics are captured.
  i2s_std_slot_config_t slot_cfg = I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(
      I2S_DATA_BIT_WIDTH_16BIT, I2S_SLOT_MODE_STEREO);
  i2s_std_config_t std_cfg = {
      .clk_cfg  = clk_cfg,
      .slot_cfg = slot_cfg,
      .gpio_cfg = gpio_cfg,
  };
  ESP_RETURN_ON_ERROR(i2s_channel_init_std_mode(s_rx_chan, &std_cfg), TAG, "init std rx");
  ESP_RETURN_ON_ERROR(i2s_channel_enable(s_rx_chan), TAG, "enable");
  ESP_LOGI(TAG, "I2S STD RX up: %d Hz stereo, bclk=%d ws=%d din=%d primary=ch%d",
           SAMPLE_RATE, I2S_BCK_GPIO, I2S_WS_GPIO, I2S_DATA_GPIO, PRIMARY_CHANNEL);
  return ESP_OK;
}

esp_err_t audio_capture_read_stereo(int16_t *primary, int16_t *reference) {
  // One 20 ms stereo frame: 320 L + 320 R = 640 int16 = 1280 bytes, interleaved.
  static int16_t stereo[FRAME_SAMPLES * 2];
  size_t want = FRAME_SAMPLES * 2 * sizeof(int16_t);
  size_t bytes_read = 0;
  esp_err_t err = i2s_channel_read(s_rx_chan, stereo, want, &bytes_read, portMAX_DELAY);
  if (err != ESP_OK) return err;
  if (bytes_read != want) return ESP_ERR_INVALID_SIZE;

  // Deinterleave: channel 0 = left, channel 1 = right.
  for (size_t i = 0; i < FRAME_SAMPLES; i++) {
    int16_t l = stereo[2 * i];
    int16_t r = stereo[2 * i + 1];
    if (PRIMARY_CHANNEL == 0) {
      primary[i] = l;
      reference[i] = r;
    } else {
      primary[i] = r;
      reference[i] = l;
    }
  }
  return ESP_OK;
}