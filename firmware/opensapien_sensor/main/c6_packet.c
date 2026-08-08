#include "c6_packet.h"

#include "config.h"

#include <string.h>

size_t c6_write_packet(uint8_t *out, size_t cap,
                       uint8_t ptype, uint32_t chunk_seq, uint32_t rel_ts_ms,
                       uint8_t vad_state, uint8_t flags,
                       const c6_frame_t *frames, size_t frame_count) {
  if (frame_count > 0xFF) {
    return 0;  /* frame_count must fit in one byte */
  }

  /* Size up front so we never write past `cap`. */
  size_t total = C6_HEADER_LEN;
  for (size_t i = 0; i < frame_count; i++) {
    total += (size_t)1 + frames[i].len;  /* [u8 len][payload] */
  }
  if (total > cap) {
    return 0;
  }

  size_t off = 0;
  out[off++] = (uint8_t)(((C6_VERSION & 0x0F) << 4) | (ptype & 0x0F));
  memcpy(out + off, &chunk_seq, 4);  off += 4;   /* little-endian */
  memcpy(out + off, &rel_ts_ms, 4);  off += 4;   /* little-endian */
  out[off++] = vad_state;
  out[off++] = (uint8_t)frame_count;
  out[off++] = flags;

  for (size_t i = 0; i < frame_count; i++) {
    out[off++] = frames[i].len;
    memcpy(out + off, frames[i].data, frames[i].len);
    off += frames[i].len;
  }
  return off;
}
