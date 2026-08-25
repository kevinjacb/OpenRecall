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

static QueueHandle_t s_replay_queue;

QueueHandle_t ble_drain_replay_queue(void) { return s_replay_queue; }

// Per-chunk scratch: enough for the worst-case 1 s of audio (50 * (1 + 255) + 12).
#define PACKET_BUF_CAP  (C6_HEADER_LEN + C6_FRAMES_PER_CHUNK * (1 + MAX_OPUS_BYTES))

/* Emit the last `seconds` of ring frames as C6_MEMORY_CHUNK packets, continuing the
 * same monotonic chunk_seq the live path uses, with C6_FLAG_LAST_OF_REQ on the final
 * packet. Single owner of chunk_seq + notify => runs here on the drain task.
 *
 * The packetization mirrors the live path's MTU-sizing but is self-contained (no
 * shared helper) to avoid touching the proven live drain loop. The window math
 * agrees with replay_window() in executor_core.c (host-tested). */
static void drain_replay(uint32_t seconds, uint32_t *chunk_seq, uint8_t *pkt, size_t pkt_cap) {
  /* Scratch for copied frame data. Static (not on the 16 KB stack) — single drain
   * task, single instance, no reentrancy, matching the pkt[] pattern in drain_task.
   * PACKET_BUF_CAP - C6_HEADER_LEN == C6_FRAMES_PER_CHUNK * (1 + MAX_OPUS_BYTES),
   * which is >= any runtime budget (= max_payload - C6_HEADER_LEN), so it always
   * holds one outer-iteration's worth of copied data.
   * Static-reuse safety: the inner while(k<count) shipping loop calls
   * c6_write_packet (which memcpys frame data into pkt synchronously) for ALL of
   * this outer iteration's frames[] before the outer loop iterates and resets
   * data_off, so no outer iteration overwrites data_scratch while a prior
   * iteration's sub-packets are still being read. */
  static uint8_t data_scratch[PACKET_BUF_CAP - C6_HEADER_LEN];

  uint32_t write_idx = ring_buffer_write_index();
  uint32_t want = seconds * (1000u / FRAME_MS);   /* seconds * 50 */
  uint32_t N = want;
  if (N > RING_FRAMES) N = RING_FRAMES;
  if (N > write_idx)   N = write_idx;
  uint32_t start_idx = write_idx - N;
  if (want != N) {
    ESP_LOGI(TAG, "replay capped: requested %u s, have %u frames", (unsigned)seconds, (unsigned)N);
  }

  uint16_t mtu = ble_link_att_mtu();
  size_t max_payload = (mtu >= 23) ? (size_t)(mtu - 3) : 244;
  size_t budget = max_payload > C6_HEADER_LEN ? max_payload - C6_HEADER_LEN : 0;

  bool emitted_any = false;
  uint32_t i = 0;
  while (i < N) {
    c6_frame_t   frames[C6_FRAMES_PER_CHUNK];
    uint8_t      frame_vad[C6_FRAMES_PER_CHUNK];
    uint32_t     frame_rel_ts[C6_FRAMES_PER_CHUNK];
    uint8_t      count = 0;
    uint32_t     first_rel_ts = 0;
    size_t       used = 0;
    /* data_off accumulates only f.len (data bytes, not the 1-byte len tag), while
     * `used` accumulates 1+f.len, so data_off <= used <= budget <= sizeof(data_scratch). */
    size_t       data_off = 0;

    while (i < N) {
      uint32_t idx = start_idx + i;
      ring_frame_t f;
      if (!ring_buffer_get_copy(idx, &f, &data_scratch[data_off])) {
        /* overwritten mid-replay: suppress (chunk_seq still advances via packets) */
        i++;
        continue;
      }
      if (count == 0) first_rel_ts = f.rel_ts_ms;
      if (f.vad_state == C6_GAP_MARKER || f.len == 0) {
        i++;            /* suppress silence in body, like the live path */
        continue;
      }
      if (used + 1u + (size_t)f.len > budget) {
        if (count == 0) {
          /* single frame bigger than budget: drop it (matches live guard) */
          ESP_LOGW(TAG, "replay frame %u len %u > budget %u; dropping",
                   (unsigned)i, (unsigned)f.len, (unsigned)budget);
          i++;
        }
        break;          /* flush this packet, start a new one */
      }
      frames[count] = (c6_frame_t){ .data = f.data, .len = f.len };
      frame_vad[count] = f.vad_state;
      frame_rel_ts[count] = f.rel_ts_ms;
      used += 1u + (size_t)f.len;
      data_off += f.len;   /* advance for next frame's copy target */
      count++;
      i++;
    }

    bool last = (i >= N);
    uint8_t flags = last ? C6_FLAG_LAST_OF_REQ : 0;

    if (count == 0) {
      /* all-silence (or overwritten) span: emit one empty packet so chunk_seq
         advances and the request boundary is marked. */
      size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                                 first_rel_ts, (uint8_t)C6_GAP_MARKER, flags, frames, 0);
      if (n > 0) ble_link_notify_audio(pkt, n);
      (*chunk_seq)++;
      emitted_any = true;
    } else {
      size_t k = 0;
      while (k < count) {
        size_t start = k, seg_used = 0;
        while (k < count && seg_used + 1u + (size_t)frames[k].len <= budget) {
          seg_used += 1u + (size_t)frames[k].len;
          k++;
        }
        size_t n_in_pkt = k - start;
        if (n_in_pkt == 0) { k++; continue; }   /* unreachable given the outer check */
        bool pkt_last = last && (k >= count);
        uint8_t fl = pkt_last ? C6_FLAG_LAST_OF_REQ : 0;
        size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                                   frame_rel_ts[start], frame_vad[start], fl,
                                   &frames[start], n_in_pkt);
        if (n == 0) {
          ESP_LOGE(TAG, "replay packet overflow: n_in_pkt=%u cap=%u",
                   (unsigned)n_in_pkt, (unsigned)pkt_cap);
        } else {
          ble_link_notify_audio(pkt, n);
        }
        (*chunk_seq)++;
      }
      emitted_any = true;
    }
  }

  if (!emitted_any) {
    /* N == 0 (e.g. seconds=0 path / empty ring): emit one boundary packet. */
    size_t n = c6_write_packet(pkt, pkt_cap, C6_MEMORY_CHUNK, *chunk_seq,
                               0, (uint8_t)C6_GAP_MARKER, C6_FLAG_LAST_OF_REQ, NULL, 0);
    if (n > 0) ble_link_notify_audio(pkt, n);
    (*chunk_seq)++;
  }
  ESP_LOGI(TAG, "replay done: seconds=%u frames=%u chunk_seq->%u",
           (unsigned)seconds, (unsigned)N, (unsigned)*chunk_seq);
}

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
    // Service any pending request_buffer replays before live draining. Replays
    // run to completion here (single owner of chunk_seq + notify), so live audio
    // is briefly delayed for the replay's duration (well under a second).
    replay_request_t rr;
    while (xQueueReceive(s_replay_queue, &rr, 0) == pdPASS) {
      drain_replay(rr.seconds, &chunk_seq, pkt, sizeof pkt);
    }

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
      uint32_t notify_count = 0;  // for burst pacing
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
          chunk_seq++;
          continue;
        }

        // Pace the burst: yield every few notifies so the stack reclaims mbufs
        // between sends and a full 200 ms chunk (up to 10 notifications) doesn't
        // exhaust the pool all at once. Safe to block the drain task ~1 ms here
        // — audio capture runs on a separate task and the 60 s PSRAM ring
        // absorbs this pause.
        if (notify_count > 0 && (notify_count % 4) == 0) {
          vTaskDelay(pdMS_TO_TICKS(1));
        }
        notify_count++;

        int rc = ble_link_notify_audio(pkt, n);
        if (rc == 0) {
          // Delivered to the stack.
        } else if (rc == -1) {
          // mbuf exhausted: the NimBLE pool is momentarily full. Retry a bounded
          // number of times with a short delay so the stack can reclaim mbufs —
          // this is the real losslessness win. Safe to block the drain task
          // ~6 ms here: capture runs on a separate task and the 60 s PSRAM ring
          // absorbs the pause.
          bool delivered = false;
          int last_rc = -1;
          for (int attempt = 0; attempt < 3; attempt++) {
            vTaskDelay(pdMS_TO_TICKS(2));
            last_rc = ble_link_notify_audio(pkt, n);
            if (last_rc == 0) { delivered = true; break; }
            if (last_rc != -1) { break; }  // link gone; stop retrying mbuf path
          }
          if (!delivered) {
            if (last_rc == -1) {
              // All retries still mbuf-exhausted: the chunk is lost on the BLE
              // link. ADVANCE chunk_seq (leaving it stuck would collide on the
              // next chunk) and log visibly at WARN — the server detects the
              // gap via the chunk_seq jump and skips it after its gap timeout
              // (handled server-side). No gap-marker packet is emitted: a
              // BLE-dropped packet never reached the Android relay, so its
              // ring buffer cannot backfill it (R5).
              ESP_LOGW(TAG, "mbuf exhausted; lost chunk_seq=%" PRIu32,
                       (uint32_t)chunk_seq);
            } else {
              // Link dropped mid-retry: treat as no-subscriber.
              ESP_LOGW(TAG, "no subscriber; link down; advance chunk_seq=%" PRIu32,
                       (uint32_t)chunk_seq);
            }
          }
        } else {
          // rc == -2: no subscriber (phone not connected / not subscribed, or
          // the notify call failed with a link error). Not an error to retry —
          // there is no one receiving and no one to backfill to. Advance
          // chunk_seq and keep draining so the ring never stalls.
          ESP_LOGW(TAG, "no subscriber; link down; advance chunk_seq=%" PRIu32,
                   (uint32_t)chunk_seq);
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
  s_replay_queue = xQueueCreate(DRAIN_REPLAY_QUEUE_DEPTH, sizeof(replay_request_t));
  if (s_replay_queue == NULL) {
    ESP_LOGE(TAG, "replay queue create failed");
    return ESP_FAIL;
  }
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
