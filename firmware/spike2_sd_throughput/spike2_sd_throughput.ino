/*
 * Phase 0 — Spike 2: microSD (SPI) sustained write throughput on XIAO ESP32S3 Sense
 * --------------------------------------------------------------------------------
 * QUESTION:  Real sustained MB/s writing to the actual SD card over the XIAO Sense's
 *            SPI bus, and the MJPEG fps/resolution that implies for Recording Mode.
 *            The XIAO Sense SD is SPI (not 4-bit SDMMC), so this is the known
 *            video bottleneck — measure it before promising any video spec.
 *
 * METHOD:    Write a large file in several chunk sizes, timing each run, and report
 *            sustained MB/s. Then derive achievable MJPEG fps for typical JPEG sizes.
 *
 * PASS:      >= 1.0 MB/s sustained  -> VGA MJPEG @ ~8-10 fps is viable.
 * KILL/PIVOT: < 0.5 MB/s -> drop video res/fps, or buffer frames to PSRAM and
 *            burst-write. Sets realistic Recording Mode scope.
 *
 * !!! PIN CONFIG — VERIFY AGAINST THE CURRENT SEEED WIKI BEFORE RUNNING !!!
 * Wrong SD pins are the #1 cause of "card mount failed" on the XIAO Sense. The
 * values below are the commonly-documented defaults for the Sense expansion board;
 * confirm them on https://wiki.seeedstudio.com/xiao_esp32s3_sense_filesystem/
 *
 * BOARD: "XIAO_ESP32S3", PSRAM enabled. Use an industrial/high-endurance card.
 * NOTE:  NOT compiled/flashed by its author — verify on device.
 */

#include "FS.h"
#include "SD.h"
#include "SPI.h"

// ---- VERIFY THESE PINS (Seeed XIAO ESP32S3 Sense SD over SPI) ----
#define SD_SCK   7
#define SD_MISO  8
#define SD_MOSI  9
#define SD_CS    21

// ---- Test config ----
// The previous run hit ~0.4 MB/s — that is exactly the 4 MHz default-clock
// ceiling (4 MHz / 8 = 0.5 MB/s), NOT the card. So we sweep the SPI clock and
// also record the worst single-write STALL, which is what actually drops video
// frames (cards periodically pause for internal GC; average MB/s hides it).
static const size_t   TOTAL_BYTES = 16UL * 1024 * 1024;  // 16 MB per run
static const size_t   CHUNK       = 32768;               // best chunk from prior run
static const uint32_t FREQS_HZ[]  = {4000000, 10000000, 20000000, 26000000, 40000000};
static const int      NUM_FREQS   = sizeof(FREQS_HZ) / sizeof(FREQS_HZ[0]);
static const float    PASS_MBPS   = 1.0f;

static uint8_t        buf[CHUNK];

struct WriteResult { float mbps; uint32_t maxStallMs; };

static bool beginAt(uint32_t freqHz) {
  // Retry: the very first begin() after power-up is sometimes flaky.
  for (int attempt = 0; attempt < 3; attempt++) {
    if (SD.begin(SD_CS, SPI, freqHz)) return true;
    SD.end();
    delay(50);
  }
  return false;
}

static WriteResult runWrite() {
  const char *path = "/spike2_test.bin";
  SD.remove(path);
  File f = SD.open(path, FILE_WRITE);
  if (!f) { Serial.println(F("    open for write FAILED")); return {-1.0f, 0}; }

  size_t written = 0;
  uint32_t maxStall = 0;
  uint32_t t0 = millis();
  while (written < TOTAL_BYTES) {
    uint32_t w0 = millis();
    size_t w = f.write(buf, CHUNK);
    uint32_t dw = millis() - w0;        // this single write()'s latency
    if (dw > maxStall) maxStall = dw;   // worst-case stall == frame-drop risk
    if (w != CHUNK) { Serial.println(F("    short write — card full/error")); break; }
    written += w;
  }
  f.flush();
  f.close();
  uint32_t dt = millis() - t0;
  SD.remove(path);

  float mbps = (written / (1024.0f * 1024.0f)) / (dt / 1000.0f);
  return {mbps, maxStall};
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println(F("=== Spike 2: microSD (SPI) write throughput vs SPI clock ==="));

  // Non-trivial payload (some controllers special-case zero pages).
  for (size_t i = 0; i < CHUNK; i++) buf[i] = (uint8_t)(i * 31 + 7);

  SPI.begin(SD_SCK, SD_MISO, SD_MOSI, SD_CS);

  Serial.println(F("SPI MHz |   MB/s | max stall | VGA fps@40KB | QVGA fps@20KB"));
  Serial.println(F("--------+--------+-----------+--------------+--------------"));

  float best = 0.0f;
  uint32_t bestFreq = 0, bestStall = 0;
  for (int i = 0; i < NUM_FREQS; i++) {
    if (!beginAt(FREQS_HZ[i])) {
      Serial.printf(" %6lu | begin() failed (clock too high for this card/wiring?)\n",
                    (unsigned long)(FREQS_HZ[i] / 1000000));
      continue;
    }
    if (i == 0) {  // report card identity once
      Serial.printf("  card type=%d, size=%llu MB\n",
                    SD.cardType(), SD.cardSize() / (1024ULL * 1024ULL));
    }
    WriteResult r = runWrite();
    SD.end();
    if (r.mbps < 0) continue;

    Serial.printf(" %6lu | %6.3f | %6lu ms | %12.1f | %12.1f\n",
                  (unsigned long)(FREQS_HZ[i] / 1000000), r.mbps,
                  (unsigned long)r.maxStallMs,
                  r.mbps * 1024.0f / 40.0f, r.mbps * 1024.0f / 20.0f);
    if (r.mbps > best) { best = r.mbps; bestFreq = FREQS_HZ[i]; bestStall = r.maxStallMs; }
  }

  Serial.printf("\nBest: %.3f MB/s @ %lu MHz  (worst stall %lu ms)\n",
                best, (unsigned long)(bestFreq / 1000000), (unsigned long)bestStall);
  bool pass = best >= PASS_MBPS;
  Serial.printf(">>> SPIKE 2 VERDICT: %s  (pass if >= %.1f MB/s)\n",
                pass ? "PASS ✅" : "FAIL ❌ -> reduce res/fps or PSRAM burst-buffer",
                PASS_MBPS);
  Serial.println(F("Note: a large worst-stall (>~100 ms) means you MUST buffer frames"));
  Serial.println(F("in PSRAM and write in bursts, regardless of average MB/s."));
  Serial.println(F("Record best MB/s, its clock, and worst stall on the scorecard."));
}

void loop() { delay(10000); }
