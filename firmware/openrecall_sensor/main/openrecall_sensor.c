/*
 * OpenRecall — application entry (ESP-IDF).
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
#include "audio_gate.h"
#include "ble_drain.h"
#include "boot_id.h"
#include "camera.h"
#include "ble_link.h"
#include "commands.h"
#include "config.h"
#include "executor.h"
#include "opus_stream.h"
#include "provisioning.h"
#include "provisioning_core.h"
#include "ring_buffer.h"
#include "snapshot.h"
#include "vad.h"

#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "nvs_flash.h"

#include <inttypes.h>  // PRIu32 — fixed-width format specifier for uint32_t (Xtensa)
#include <stdbool.h>   // bool / true / false for the DSP adapt gate

static const char *TAG = "sense";

/* Monotonic ms clock shared with the snapshot/video code (core 0). The audio
 * task (core 1) updates this each frame from its local rel_ts_ms; a 32-bit
 * aligned volatile write is atomic on Xtensa, so the timer-task reader
 * (rel_ts_ms_now) needs no lock. See snapshot.h. */
static volatile uint32_t s_rel_ts_ms = 0;

uint32_t rel_ts_ms_now(void) { return s_rel_ts_ms; }

// Core-1 audio task: capture -> single-mic energy VAD -> Opus encode
// -> ring_buffer push. rel_ts_ms is a monotonic per-device millisecond counter
// (not wall clock — the device may not have synced time). It starts at 0 at
// boot and advances by FRAME_MS per frame; the server uses it to lay out
// transcript windows.
static void audio_task(void *arg) {
  (void)arg;
  static int16_t pri[FRAME_SAMPLES];   // voice/primary mic (left, faces mouth)
  static int16_t ref[FRAME_SAMPLES];   // reference mic (right, faces away) — cal log only
  static uint8_t opus_buf[MAX_OPUS_BYTES];
  vad_t vad;
  vad_init(&vad, VAD_ENERGY_THRESHOLD, VAD_HANGOVER_FRAMES);

  uint32_t frames = 0, voiced = 0, gaps = 0, total = 0;
  uint32_t rel_ts_ms = 0;
  // DEBUG (dual-mic bring-up): per-second primary/reference energy + ratio.
  // Distinguishes L/R-channel inversion (e_ref >> e_pri during speech) from
  // no-capture (both ~0) from threshold too high (e_pri < 2,000,000) from
  // slot-width garbage (erratic). Remove once tuned.
  uint64_t acc_pri = 0, acc_ref = 0;
  // Once-per-minute stack high-water mark. First call here is right after the
  // first opus_encode, so we see real steady-state usage. We log every 3000
  // frames (60 s) which is cheap and lets us size the stack to the true
  // peak + a safety margin on a later boot.
  uint32_t stack_report_at = 3000;
  for (;;) {
    if (audio_capture_read_stereo(pri, ref) != ESP_OK) {
      continue;  // DMA not ready yet; retry next tick
    }

    if (audio_gate_paused()) {
      // stop_audio: drain DMA (read above) but capture no speech. Push a gap
      // marker so the ring stays contiguous (chunk_seq + rel_ts keep advancing
      // via the drainer's empty packets). Skip energy/VAD/encode; fall through to
      // the shared per-frame tail so monitoring (per-second log, stack high-water)
      // stays identical to the live path.
      ring_buffer_push(C6_GAP_MARKER, rel_ts_ms, NULL, 0);
      gaps++;
      goto frame_tail;
    }

    // DEBUG: accumulate per-sample energy of each channel for the per-second
    // calibration log. int32 squared into uint64 — 320 samples, no overflow.
    for (int i = 0; i < FRAME_SAMPLES; i++) {
      int32_t p = pri[i], r = ref[i];
      acc_pri += (uint64_t)(p * p);
      acc_ref += (uint64_t)(r * r);
    }

    // Single-mic energy VAD on the primary (mouth) mic. The dual-channel ratio
    // gate + NLMS canceller are intentionally NOT used: two omnidirectional
    // INMP441s with insufficient acoustic shadowing both hear the wearer's voice
    // at ~equal level (ratio ~1), so the ratio gate would reject the voice and
    // the canceller would adapt to subtract it. Encode the primary directly —
    // same behaviour as the single-mic inbuilt PDM mic. The reference channel is
    // still captured for the per-second cal log below.
    uint8_t state = vad_process_single(&vad, pri, FRAME_SAMPLES);

    if (state == C6_SPEECH || state == C6_HANGOVER) {
      // Voiced frame: encode the primary mic and push the Opus packet. A
      // failure here is fatal to the frame (it'll show up as a chunk_seq gap on
      // the server, which the request_chunks + ring-buffer replay can recover).
      int n = opus_stream_encode(pri, opus_buf, sizeof opus_buf);
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

  frame_tail:
    frames++;
    rel_ts_ms += FRAME_MS;
    s_rel_ts_ms = rel_ts_ms;   /* publish to the shared clock (snapshot/video) */

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
      // DEBUG cal: e_pri/e_ref are summed per-sample-square over the second.
      // ratio = e_pri/e_ref. If voice is on primary, e_pri >> e_ref while speaking
      // (ratio high). If voice is on the reference channel (L/R swapped), e_ref
      // >> e_pri while speaking (ratio < 1). If a mic is dead, its channel ~0.
      uint64_t ratio = acc_ref ? acc_pri / acc_ref : 0;
      ESP_LOGI(TAG, "cal e_pri=%llu e_ref=%llu ratio=%llu",
               (unsigned long long)acc_pri, (unsigned long long)acc_ref,
               (unsigned long long)ratio);
      total = 0;
      acc_pri = 0;
      acc_ref = 0;
    }
  }
}

void app_main(void) {
  ESP_LOGI(TAG, "OpenRecall booting");
  ESP_LOGI(TAG, "audio: %d Hz stereo (dual-mic), %d ms frames (%d samples), Opus %d bps cplx %d",
           SAMPLE_RATE, FRAME_MS, FRAME_SAMPLES, OPUS_BITRATE, OPUS_COMPLEXITY);

  // NimBLE needs NVS for its keystore.
  esp_err_t nv = nvs_flash_init();
  if (nv == ESP_ERR_NVS_NO_FREE_PAGES || nv == ESP_ERR_NVS_NEW_VERSION_FOUND) {
    ESP_ERROR_CHECK(nvs_flash_erase());
    nv = nvs_flash_init();
  }
  ESP_ERROR_CHECK(nv);

  // Per-activate counter (disambiguates rel_ts_ms across boots; SoftAP SSID).
  boot_id_init();

  ESP_ERROR_CHECK(ring_buffer_init());
  ESP_ERROR_CHECK(audio_capture_init());
  ESP_ERROR_CHECK(opus_stream_init());

  // provision happens here. 
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

  if (executor_init() != ESP_OK) {
    ESP_LOGE(TAG, "executor_init failed");
  }

  /* P4b camera + ambient snapshot. camera_init configures the OV2640; the SD
   * store mounts lazily inside snapshot_capture_one. snapshot_init creates and
   * starts the auto-reload timer at SNAPSHOT_INTERVAL_S (the server can override
   * via snapshot_set_interval through the executor). */
  if (camera_init() != ESP_OK) {
    ESP_LOGE(TAG, "camera_init failed — snapshots disabled");
  } else if (snapshot_init() != ESP_OK) {
    ESP_LOGE(TAG, "snapshot_init failed");
  }

  // Audio pinned to core 1; NimBLE host task + drainer on core 0.
  // 32 KB stack. 8 KB overflowed at boot; 16 KB overflowed at boot. Rather
  // than guess, 32 KB is enough headroom to be safe, and audio_task logs
  // uxTaskGetStackHighWaterMark once a minute so the next reflash can size
  // this back to the true peak + ~2 KB safety margin. The drainer on core 0
  // doesn't call into Opus, so 4 KB is fine for it.
  xTaskCreatePinnedToCore(audio_task, "audio", 32768, NULL, 5, NULL, 1);
  // Drain starts before the link so the replay queue + drain task exist before
  // the link can accept a request_buffer command (otherwise the executor would
  // log "drain replay queue not ready — dropped").
  ESP_ERROR_CHECK(ble_drain_start());
  ESP_ERROR_CHECK(ble_link_start(commands_handle));

  ESP_LOGI(TAG, "capture + VAD + Opus on core 1, drain + BLE advertising on core 0, "
                "§D verify ready");
}
