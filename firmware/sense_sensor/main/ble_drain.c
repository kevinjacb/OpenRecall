/*
 * §C.6 stream drainer — implementation.
 *
 * See ble_drain.h for the design. One FreeRTOS task on core 0, sleeping 10 ms
 * between checks. Drains in chunks of C6_FRAMES_PER_CHUNK frames; preroll on
 * C6_SPEECH onset; gap markers emitted as zero-frame body packets so chunk_seq
 * keeps advancing.
 */
#include "ble_drain.h"

#include "ble_link.h"
#include "c6_packet.h"
#include "config.h"
#include "ring_buffer.h"

#include "esp_check.h"
#include "esp_log.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#include <inttypes.h>  // PRIu32 — fixed-width format specifier for uint32_t (Xtensa)

static const char *TAG = "drain";

// Per-chunk scratch: enough for the worst-case 1 s of audio (50 * (1 + 255) + 12).
#define PACKET_BUF_CAP  (C6_HEADER_LEN + C6_FRAMES_PER_CHUNK * (1 + MAX_OPUS_BYTES))

static void drain_task(void *arg) {
  (void)arg;
  // Static so the ~13 KB scratch lives in BSS, not on the 16 KB task stack
  // (single drain task, single instance — no reentrancy). The build loop
  // below reuses it for each MTU-sized sub-packet.
  static uint8_t pkt[PACKET_BUF_CAP];
  uint32_t cursor = 0;       // next absolute ring index we'll read
  uint32_t chunk_seq = 0;
  bool     in_speech = false;  // last batch ended in voiced/hangover; gates preroll
  // Once-per-minute stack high-water mark. NimBLE's notify path (L2CAP → GATT
  // → ATT → HCI) eats more stack than the audio path; 4 KB overflowed on
  // Xtensa LX7. We log here so the next reflash can size the stack to the
  // true peak + 2 KB instead of guessing.
  uint32_t stack_report_at = 3000;

  ESP_LOGI(TAG, "drainer up: chunk=%d frames, gap markers carried as empty packets",
           C6_FRAMES_PER_CHUNK);

  for (;;) {
    uint32_t write_idx = ring_buffer_write_index();
    uint32_t available = write_idx - cursor;
    if (available < C6_FRAMES_PER_CHUNK) {
      vTaskDelay(pdMS_TO_TICKS(10));
      continue;
    }

    // ---- VAD preroll: rewind on voiced onset so the first packet of an utterance
    //      carries ~300 ms of pre-onset context. Rewind is bounded by the ring's
    //      valid window; if those frames have already been overwritten, skip it.
    bool     prerolled = false;
    uint32_t preroll_base = cursor;
    if (!in_speech) {
      ring_frame_t head;
      uint32_t head_idx = cursor;
      if (ring_buffer_get(head_idx, &head) &&
          (head.vad_state == C6_SPEECH || head.vad_state == C6_HANGOVER)) {
        uint32_t rewind_to = (head_idx >= VAD_PREROLL_FRAMES)
                                ? head_idx - VAD_PREROLL_FRAMES
                                : 0;
        ring_frame_t check;
        if (ring_buffer_get(rewind_to, &check)) {
          uint32_t actual_rewind = head_idx - rewind_to;
          if (actual_rewind > 0) {
            cursor = rewind_to;
            preroll_base = cursor;
            prerolled = true;
            // Recompute availability; if we still don't have a full chunk
            // worth, wait — better to emit on the next tick than to ship
            // a short packet.
            available = write_idx - cursor;
            if (available < C6_FRAMES_PER_CHUNK) {
              vTaskDelay(pdMS_TO_TICKS(10));
              continue;
            }
          }
        }
      }
    }

    // ---- Collect the next C6_FRAMES_PER_CHUNK ring frames (drop gap markers) ----
    // Pack them into one or more §C.6 packets sized to the negotiated ATT MTU:
    // a BLE notification is a single PDU of at most (mtu - 3) bytes and does NOT
    // fragment, so a larger packet is silently truncated on the air — the phone
    // forwards the truncated fragment and the server rejects it ("frame N
    // truncated"). We emit as many MTU-sized packets as needed, each its own
    // chunk_seq + the first frame's rel_ts/vad. The phone and server treat each
    // notification as one packet, so they need no changes.
    c6_frame_t frames[C6_FRAMES_PER_CHUNK];
    uint8_t  frame_vad[C6_FRAMES_PER_CHUNK];    // per-frame vad state, for the header
    uint32_t frame_rel_ts[C6_FRAMES_PER_CHUNK]; // per-frame rel_ts, for each packet's header
    uint8_t  count = 0;
    uint32_t first_rel_ts_ms = 0;               // chunk's first ring frame (silence packet)
    bool     has_audio = false;                 // did we include at least one Opus frame?

    for (uint32_t i = 0; i < C6_FRAMES_PER_CHUNK; i++) {
      uint32_t idx = cursor + i;
      ring_frame_t f;
      if (!ring_buffer_get(idx, &f)) {
        // Should not happen (we checked available >= chunk size) but be safe.
        break;
      }
      if (i == 0) {
        first_rel_ts_ms = f.rel_ts_ms;
      }
      if (f.vad_state == C6_GAP_MARKER || f.len == 0) {
        // Suppress silence; the packet still carries chunk_seq + rel_ts below.
        continue;
      }
      frames[count] = (c6_frame_t){ .data = f.data, .len = f.len };
      // Preroll tag overrides the frame's own VAD state for the header byte.
      frame_vad[count] = prerolled && ((idx - preroll_base) < VAD_PREROLL_FRAMES)
                           ? (uint8_t)C6_PREROLL
                           : f.vad_state;
      frame_rel_ts[count] = f.rel_ts_ms;
      count++;
      has_audio = true;
    }

    // Per-packet body budget. Fall back to the Android-requested 247-MTU shape
    // (244 payload) if the MTU isn't known yet — the phone requests 247 on
    // connect, so this is the steady state; the fallback only covers the brief
    // window before the exchange completes.
    uint16_t mtu = ble_link_att_mtu();
    size_t max_payload = (mtu >= 23) ? (size_t)(mtu - 3) : 244;
    size_t budget = max_payload > C6_HEADER_LEN ? max_payload - C6_HEADER_LEN : 0;

    if (count == 0) {
      // The whole batch is silence (long gap). Emit one empty packet so
      // chunk_seq and rel_ts keep advancing (the server's reassembler relies on
      // monotonic chunk_seq). frame_count=0 → no body, fits any MTU.
      size_t n = c6_write_packet(pkt, sizeof(pkt), C6_LIVE, chunk_seq,
                                 first_rel_ts_ms, (uint8_t)C6_GAP_MARKER, 0,
                                 frames, 0);
      if (n > 0) {
        ble_link_notify_audio(pkt, n);  // dropped if no subscriber; fine
      }
      chunk_seq++;
    } else {
      // Pack frames into MTU-sized packets. Each packet carries as many frames
      // as fit; chunk_seq advances per packet so the server sees monotonic seq.
      size_t i = 0;
      while (i < count) {
        size_t start = i;
        size_t used = 0;
        while (i < count && used + 1 + (size_t)frames[i].len <= budget) {
          used += 1 + (size_t)frames[i].len;
          i++;
        }
        size_t n_in_pkt = i - start;
        if (n_in_pkt == 0) {
          // Even this one frame (frames[i]) doesn't fit the budget — the Opus
          // frame is larger than (mtu - 3 - header - 1). Drop it so we never
          // ship a truncated notification; the encoder cap should keep frames
          // well under this, but guard against a transient or a tiny MTU.
          ESP_LOGW(TAG, "frame %u len %u > budget %u (mtu=%u); dropping",
                   (unsigned)i, (unsigned)frames[i].len,
                   (unsigned)budget, (unsigned)mtu);
          i++;
          continue;
        }
        size_t n = c6_write_packet(pkt, sizeof(pkt), C6_LIVE, chunk_seq,
                                   frame_rel_ts[start], frame_vad[start], 0,
                                   &frames[start], n_in_pkt);
        if (n == 0) {
          ESP_LOGE(TAG, "packet overflow: n_in_pkt=%u cap=%u",
                   (unsigned)n_in_pkt, (unsigned)sizeof(pkt));
        } else {
          // ble_link_notify_audio is non-blocking and returns <0 if the phone
          // isn't subscribed — that's fine; we keep draining so the ring never
          // stalls.
          int rc = ble_link_notify_audio(pkt, n);
          if (rc < 0) {
            ESP_LOGD(TAG, "no subscriber; dropped chunk %" PRIu32, (uint32_t)chunk_seq);
          }
        }
        chunk_seq++;
      }
    }

    cursor += C6_FRAMES_PER_CHUNK;
    // Track speech state for the next batch's preroll decision.
    if (has_audio) {
      in_speech = (frame_vad[count - 1] == C6_SPEECH ||
                   frame_vad[count - 1] == C6_HANGOVER);
    } else {
      in_speech = false;
    }

    if (chunk_seq == stack_report_at) {
      // UBaseType_t is unsigned; cast to uint32_t for the format string.
      // 16384 is the drain task stack size from xTaskCreatePinnedToCore() in
      // ble_drain_start().
      uint32_t hw = (uint32_t)uxTaskGetStackHighWaterMark(NULL);
      ESP_LOGI(TAG, "drain stack high-water mark: %" PRIu32 " bytes free (stack size %" PRIu32 ")",
               hw, (uint32_t)16384);
      stack_report_at += 3000;
    }
  }
}

esp_err_t ble_drain_start(void) {
  // Core 0 — opposite the audio task (core 1). The drainer calls into NimBLE
  // (ble_gatts_notify_custom → GATT → L2CAP → ATT → HCI), whose frame depth
  // exceeds 4 KB on Xtensa LX7. 16 KB is enough headroom; the high-water-mark
  // log below will let a follow-up reflash size this back to the true peak +
  // ~2 KB safety margin. The worst single-allocation is PACKET_BUF_CAP
  // (~13 KB) which is static, not on-stack.
  BaseType_t ok = xTaskCreatePinnedToCore(
      drain_task, "drain", 16384, NULL, 5, NULL, 0);
  ESP_RETURN_ON_FALSE(ok == pdPASS, ESP_FAIL, TAG, "xTaskCreatePinnedToCore failed");
  return ESP_OK;
}
