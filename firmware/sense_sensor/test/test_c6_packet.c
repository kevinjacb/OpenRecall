/*
 * Host-side contract test for the firmware §C.6 writer.
 *
 * Compiles and runs on the dev host (no ESP toolchain) to prove the C encoder emits
 * bytes identical to the server's Python encoder. The golden vector was produced by
 * AudioPacket(...).encode() — keep them in lockstep.
 *
 *   cc -std=c11 -I../main ../main/c6_packet.c test_c6_packet.c -o /tmp/c6test && /tmp/c6test
 */
#include "c6_packet.h"
#include "config.h"

#include <stdio.h>
#include <string.h>

/* AudioPacket(version=1, ptype=MEMORY_CHUNK, chunk_seq=412, rel_ts_ms=184213,
 * vad=SPEECH, flags=0, frames=[b"\x01\x02\x03", b"\x04\x05\x06\x07"]).encode() */
static const uint8_t GOLDEN[] = {
    0x11, 0x9c, 0x01, 0x00, 0x00, 0x95, 0xcf, 0x02, 0x00, 0x01, 0x02,
    0x00, 0x03, 0x01, 0x02, 0x03, 0x04, 0x04, 0x05, 0x06, 0x07,
};

static int failures = 0;

static void check(const char *name, int ok) {
  printf("  [%s] %s\n", ok ? "PASS" : "FAIL", name);
  if (!ok) failures++;
}

int main(void) {
  printf("§C.6 writer host contract test\n");

  const uint8_t f0[] = {0x01, 0x02, 0x03};
  const uint8_t f1[] = {0x04, 0x05, 0x06, 0x07};
  c6_frame_t frames[2] = {{f0, sizeof f0}, {f1, sizeof f1}};

  uint8_t out[64];
  size_t n = c6_write_packet(out, sizeof out, C6_MEMORY_CHUNK, 412, 184213,
                             C6_SPEECH, 0, frames, 2);

  check("length matches golden", n == sizeof GOLDEN);
  check("bytes match server encoder", n == sizeof GOLDEN && memcmp(out, GOLDEN, n) == 0);

  /* Overflow safety: a tiny buffer must be refused, not overrun. */
  uint8_t tiny[8];
  check("refuses to overflow", c6_write_packet(tiny, sizeof tiny, C6_MEMORY_CHUNK,
                                                412, 184213, C6_SPEECH, 0, frames, 2) == 0);

  /* Gap marker: header only, no frames. */
  size_t g = c6_write_packet(out, sizeof out, C6_MEMORY_CHUNK, 7, 140,
                             C6_GAP_MARKER, 0, NULL, 0);
  check("gap marker is header-only", g == C6_HEADER_LEN);

  printf(failures ? "\nFAILED (%d)\n" : "\nOK\n", failures);
  return failures ? 1 : 0;
}
