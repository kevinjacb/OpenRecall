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
 *   [x] opus_stream   — libopus encoder (Spike-1 params, complexity 1)
 *   [x] ble_drain     — core-0 drainer: ring -> §C.6 -> notify (+ VAD preroll)
 *   [x] ble_link      — NimBLE GATT (audio notify / command / ack)
 *   [x] commands      — Ed25519 verify (libsodium) + execute + ack
 */
#include "audio_capture.h"
#include "ble_drain.h"
#include "ble_link.h"
#include "commands.h"
#include "config.h"
#include "opus_stream.h"
#include "provisioning.h"
#include "provisioning_core.h"
#include "ring_buffer.h"
#include "vad.h"
#include "mic_dsp.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include <inttypes.h>  // PRIu32 — fixed-width format specifier for uint32_t (Xtensa)
#include <stdbool.h>   // bool / true / false for the DSP adapt gate

static const char *TAG = "sense";

// Core-1 audio task: capture -> dual-VAD -> NLMS cancellation -> Opus encode
// -> ring_buffer push. rel_ts_ms is a monotonic per-device millisecond counter
// (not wall clock — the device may not have synced time). It starts at 0 at
// boot and advances by FRAME_MS per frame; the server uses it to lay out
// transcript windows.
static void audio_task(void *arg) {
  (void)arg;
  static int16_t pri[FRAME_SAMPLES];   // voice/primary mic (front)
  static int16_t ref[FRAME_SAMPLES];   // noise/reference mic (back)
  static int16_t mono[FRAME_SAMPLES];  // cleaned mono output of the canceller
  static uint8_t opus_buf[MAX_OPUS_BYTES];
  vad_t vad;
  vad_init(&vad, VAD_ENERGY_THRESHOLD, VAD_HANGOVER_FRAMES);
  mic_dsp_t dsp;
  mic_dsp_init(&dsp);

  uint32_t frames = 0, voiced = 0, gaps = 0, total = 0;
  uint32_t rel_ts_ms = 0;
  // Once-per-minute stack high-water mark. First call here is right after the
  // first opus_encode, so we see real steady-state usage. We log every 3000
  // frames (60 s) which is cheap and lets us size the stack to the true
  // peak + a safety margin on a later boot.
  uint32_t stack_report_at = 3000;
  for (;;) {
    if (audio_capture_read_stereo(pri, ref) != ESP_OK) {
      continue;  // DMA not ready yet; retry next tick
    }

    // VAD first: its decision gates both the encode and the NLMS adaptation.
    uint8_t state = vad_process_dual(&vad, pri, ref, FRAME_SAMPLES);
    // Adapt the noise canceller ONLY on noise-only frames; freeze during speech
    // so the filter cancels ambient noise, not voice.
    bool adapt_now = (state == C6_GAP_MARKER);
    mic_dsp_process(&dsp, pri, ref, mono, FRAME_SAMPLES, adapt_now);

    if (state == C6_SPEECH || state == C6_HANGOVER) {
      // Voiced frame: encode the CLEANED mono and push the Opus packet. A
      // failure here is fatal to the frame (it'll show up as a chunk_seq gap on
      // the server, which the request_chunks + ring-buffer replay can recover).
      int n = opus_stream_encode(mono, opus_buf, sizeof opus_buf);
      if (n > 0 && n <= UINT8_MAX) {
        ring_buffer_push(state, rel_ts_ms, opus_buf, (uint8_t)n);
        voiced++;
        total += (uint32_t)n;
      } else {
        gaps++;  // encode error — treat as a gap so the server can backfill
      }
    } else {
      // Silence/noise: push a zero-length frame so the ring keeps a contiguous
      // record (and the drainer can preserve chunk_seq + rel_ts continuity).
      ring_buffer_push(C6_GAP_MARKER, rel_ts_ms, NULL, 0);
      gaps++;
    }

    frames++;
    rel_ts_ms += FRAME_MS;

    if (frames == stack_report_at) {
      // UBaseType_t is unsigned; cast to uint32_t for the format string.
      // 32768 is the audio task stack size from xTaskCreatePinnedToCore() below.
      uint32_t hw = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
      ESP_LOGI(TAG, "audio stack high-water mark: %" PRIu32 " bytes free (stack size %" PRIu32 ")",
               hw, (uint32_t)32768);
      stack_report_at += 3000;
    }

    if (frames % (1000 / FRAME_MS) == 0) {  // ~once per second
      ESP_LOGI(TAG, "frames=%" PRIu32 " voiced=%" PRIu32 " gap=%" PRIu32 " opus_bytes/s=%" PRIu32,
               frames, voiced, gaps, total);
      total = 0;
    }
  }
}

void app_main(void) {
  ESP_LOGI(TAG, "Sense AI Sensor booting");
  ESP_LOGI(TAG, "audio: %d Hz stereo (dual-mic), %d ms frames (%d samples), Opus %d bps cplx %d",
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
  ESP_ERROR_CHECK(opus_stream_init());

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

  // Audio pinned to core 1; NimBLE host task + drainer on core 0.
  // 32 KB stack. 8 KB overflowed at boot; 16 KB overflowed at boot. Rather
  // than guess, 32 KB is enough headroom to be safe, and audio_task logs
  // uxTaskGetStackHighWaterMark once a minute so the next reflash can size
  // this back to the true peak + ~2 KB safety margin. The drainer on core 0
  // doesn't call into Opus, so 4 KB is fine for it.
  xTaskCreatePinnedToCore(audio_task, "audio", 32768, NULL, 5, NULL, 1);
  ESP_ERROR_CHECK(ble_link_start(commands_handle));
  ESP_ERROR_CHECK(ble_drain_start());

  ESP_LOGI(TAG, "capture + VAD + Opus on core 1, drain + BLE advertising on core 0, "
                "§D verify ready");
}
