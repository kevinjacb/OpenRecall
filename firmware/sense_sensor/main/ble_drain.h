/*
 * §C.6 stream drainer — core 0 reader of the encoded-audio ring buffer.
 *
 * The producer (core-1 audio task) pushes one encoded Opus frame per 20 ms to
 * ring_buffer (with rel_ts_ms + vad_state). This task is the consumer: it owns
 * a monotonic cursor, batches frames into §C.6 packets of C6_FRAMES_PER_CHUNK
 * frames (1 s of audio), writes the packet via c6_write_packet, and notifies
 * the subscribed phone via ble_link_notify_audio.
 *
 * VAD preroll: on a transition from C6_GAP_MARKER -> C6_SPEECH/C6_PREROLL we
 * rewind the cursor by VAD_PREROLL_FRAMES so the first packet of an utterance
 * carries the ~300 ms of pre-onset context the server's reassembler expects.
 * Preroll frames are tagged C6_PREROLL in the §C.6 vad_state byte; everything
 * else uses the frame's own VAD state (C6_SPEECH / C6_HANGOVER / C6_GAP_MARKER).
 *
 * Gap markers: silence is NOT forwarded as audio. A gap-marker frame is omitted
 * from the packet body but the packet is still emitted (so the chunk_seq and
 * rel_ts_ms keep advancing, and the server can account for the silence).
 *
 * Runs on core 0 alongside the NimBLE host task. Non-blocking: if no frames are
 * available it sleeps 10 ms; if no phone is subscribed ble_link_notify_audio is
 * a no-op so we keep draining (the ring is 60 s deep — better to keep up).
 */
#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

// Create the drainer task on core 0. Call once after ring_buffer_init().
esp_err_t ble_drain_start(void);

#ifdef __cplusplus
}
#endif
