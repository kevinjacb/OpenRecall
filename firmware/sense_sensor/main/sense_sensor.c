/*
 * Sense AI Sensor — application entry (ESP-IDF).
 *
 * The wearable is deliberately dumb: capture audio, VAD-gate it, Opus-encode it,
 * buffer it in a PSRAM ring, and stream it as §C.6 over BLE; receive signed §D
 * commands, verify them against the provisioned server key, execute, and ack. It
 * does NOT do §E session framing — that is the phone relay's job (BLE <-> WS).
 *
 * Core layout (dual-core S3): core 1 = audio (capture -> VAD -> [Opus] -> ring),
 * core 0 = BLE (drain ring -> §C.6 -> notify; handle commands). Splitting encode
 * (core 1) from the radio (core 0) is why Spike 1 measured the encoder in isolation.
 *
 * Module status:
 *   [x] c6_packet     — §C.6 writer (host-tested vs server encoder)
 *   [x] audio_capture — I2S PDM RX via DMA
 *   [x] vad           — software VAD -> gap markers (host-tested)
 *   [x] ring_buffer   — 60 s PSRAM history
 *   [~] opus_stream   — written; needs a libopus component on the bench
 *   [ ] ble_link      — NimBLE GATT (audio notify / command / ack)
 *   [ ] commands      — Ed25519 verify (mbedTLS) + execute
 */
#include "audio_capture.h"
#include "ble_link.h"
#include "commands.h"
#include "config.h"
#include "provisioning.h"
#include "provisioning_core.h"
#include "ring_buffer.h"
#include "vad.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

static const char *TAG = "sense";

// Core-1 audio task: capture -> VAD -> (Opus -> ring). Until the libopus component
// is added, it captures and classifies and reports stats, proving the live path.
static void audio_task(void *arg) {
  static int16_t pcm[FRAME_SAMPLES];
  vad_t vad;
  vad_init(&vad, VAD_ENERGY_THRESHOLD, VAD_HANGOVER_FRAMES);

  uint32_t frames = 0, voiced = 0, gaps = 0;
  for (;;) {
    if (audio_capture_read_frame(pcm) != ESP_OK) {
      continue;
    }
    uint8_t state = vad_process(&vad, pcm, FRAME_SAMPLES);
    if (state == C6_SPEECH || state == C6_HANGOVER) {
      voiced++;
    } else {
      gaps++;
    }
    // TODO(opus): int n = opus_stream_encode(pcm, buf, sizeof buf);
    //             ring_buffer_push(state, rel_ts, buf, n);  // gap -> push len 0
    frames++;
    if (frames % (1000 / FRAME_MS) == 0) {  // ~once per second
      ESP_LOGI(TAG, "frames=%u voiced=%u gap=%u", frames, voiced, gaps);
    }
  }
}

void app_main(void) {
  ESP_LOGI(TAG, "Sense AI Sensor booting");
  ESP_LOGI(TAG, "audio: %d Hz mono, %d ms frames (%d samples), Opus %d bps cplx %d",
           SAMPLE_RATE, FRAME_MS, FRAME_SAMPLES, OPUS_BITRATE, OPUS_COMPLEXITY);

  // NimBLE needs NVS for its keystore.
  esp_err_t nv = nvs_flash_init();
  if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    nv = nvs_flash_init();
  }
  ESP_ERROR_CHECK(nv);

  ESP_ERROR_CHECK(ring_buffer_init());
  ESP_ERROR_CHECK(audio_capture_init());

  if (provisioning_init() != 0) {
    ESP_LOGE(TAG, "provisioning_init failed");
  }
  /* commands_init seeds the verifier; if provisioned, provisioning_init already
   * installed the real key via commands_set_pubkey. The fallback is all-zeros. */
  if (commands_init(SERVER_ED25519_PUBKEY, ble_link_notify_ack) != 0) {
    ESP_LOGE(TAG, "commands_init failed");
  }
  if (provisioning_state_byte() == 1) {
    commands_set_pubkey(provisioning_core_server_key());  /* belt-and-suspenders: ensure live key */
  }

  // Audio pinned to core 1; NimBLE host task runs on core 0.
  xTaskCreatePinnedToCore(audio_task, "audio", 4096, NULL, 5, NULL, 1);
  ESP_ERROR_CHECK(ble_link_start(commands_handle));

  ESP_LOGI(TAG, "capture + VAD on core 1, BLE advertising, §D verify ready (Opus pending)");
}
