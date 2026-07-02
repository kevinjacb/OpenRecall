/*
 * §C.6 audio packet writer (firmware side) — portable C, no ESP dependencies.
 *
 * Byte-for-byte mirror of the server's parser (sense_server/ingest/audio_packet.py)
 * and reference encoder (AudioPacket.encode). The device emits these over the BLE
 * audio characteristic; the phone forwards them verbatim to the server.
 *
 * Layout (little-endian; ESP32-S3 and host are both little-endian):
 *   byte 0      (version<<4) | ptype
 *   bytes 1-4   chunk_seq   (uint32)
 *   bytes 5-8   rel_ts_ms   (uint32)
 *   byte 9      vad_state
 *   byte 10     frame_count
 *   byte 11     flags
 *   byte 12..   repeated: [u8 len][opus bytes] x frame_count
 *
 * Kept free of ESP/IDF headers so it is unit-tested on the host against the
 * server-generated golden vector (see test/test_c6_packet.c).
 */
#pragma once

#include <stddef.h>
#include <stdint.h>

#ifdef __cplusplus
extern "C" {
#endif

/* One Opus frame to pack: pointer + length (length must be <= 255). */
typedef struct {
  const uint8_t *data;
  uint8_t        len;
} c6_frame_t;

/*
 * Serialise a §C.6 packet into `out` (capacity `cap`).
 * Returns bytes written, or 0 on overflow or if frame_count > 255.
 * Mirrors AudioPacket.encode exactly.
 */
size_t c6_write_packet(uint8_t *out, size_t cap,
                       uint8_t ptype, uint32_t chunk_seq, uint32_t rel_ts_ms,
                       uint8_t vad_state, uint8_t flags,
                       const c6_frame_t *frames, size_t frame_count);

#ifdef __cplusplus
}
#endif
