/*
 * Phase 0 — Spike 1: Opus encode load on the ESP32-S3 (XIAO ESP32S3 Sense)
 * ------------------------------------------------------------------------
 * QUESTION:  Can the S3 encode Opus 16 kHz / 24 kbps / 20 ms mono frames in
 *            real time, with comfortable headroom, while BLE runs on the other
 *            core?  This is the cheapest possible kill for the whole audio
 *            architecture — run it FIRST.
 *
 * METHOD:    Encode a realistic (noisy/speech-like, NOT silent) 20 ms frame in a
 *            tight loop, time each encode with the hardware microsecond timer,
 *            and report min / avg / p95 / max against the 20,000 us real-time
 *            budget. Silence encodes trivially fast and would flatter the result,
 *            so we feed a noise+tone mix to approximate worst-case speech.
 *
 * PASS:      avg < 10,000 us/frame AND p95 < 15,000 us/frame  (>= ~25% headroom).
 *            Encoder runs pinned to core 1; BLE/relay would run on core 0, so the
 *            measured cost is what competes with the rest of the audio task only.
 *
 * KILL/PIVOT: if it fails, fall back to ADPCM (4:1, near-free CPU) or raw PCM and
 *            lean on Android's generous BLE bandwidth. Cheap to pivot NOW.
 *
 * DEPENDENCY: Opus for Arduino — install "arduino-libopus" by Phil Schatzmann
 *   (https://github.com/pschatzmann/arduino-libopus). It vendors libopus and
 *   exposes the standard C API (opus.h) used below.
 *
 * BOARD: select "XIAO_ESP32S3", enable PSRAM (Tools > PSRAM > "OPI PSRAM").
 *        Open Serial Monitor at 115200.
 *
 * NOTE: This file has NOT been compiled/flashed by its author — verify on device.
 */

#include <Arduino.h>
#include "opus.h"

// ---- Audio config (matches the V1 spec: Opus 16 kHz mono, 24 kbps, 20 ms) ----
static const int   SAMPLE_RATE   = 16000;
static const int   CHANNELS      = 1;
static const int   FRAME_MS      = 20;
static const int   FRAME_SAMPLES = SAMPLE_RATE * FRAME_MS / 1000;  // 320
static const int   BITRATE       = 24000;
static const int   APPLICATION   = OPUS_APPLICATION_VOIP;

// ---- Measurement config ----
static const int   WARMUP_FRAMES = 50;
static const int   MEASURE_FRAMES = 1000;
static const int   REALTIME_BUDGET_US = FRAME_MS * 1000;  // 20000
static const int   PASS_AVG_US   = 10000;
static const int   PASS_P95_US   = 15000;

static int16_t     pcm[FRAME_SAMPLES];
static uint8_t     opusBuf[4000];
static uint32_t    timings[MEASURE_FRAMES];

// Fill a frame with a speech-like signal: a 300 Hz tone + pseudo-random noise.
// Re-randomised each call so the encoder cannot trivially exploit repetition.
static void fillFrame(int16_t *buf, uint32_t phase) {
  for (int i = 0; i < FRAME_SAMPLES; i++) {
    float t    = (float)(phase + i) / SAMPLE_RATE;
    int16_t tone  = (int16_t)(8000.0f * sinf(2.0f * PI * 300.0f * t));
    int16_t noise = (int16_t)((esp_random() & 0x3FFF) - 0x2000);  // +-8192
    buf[i] = (int16_t)constrain(tone + noise, -32768, 32767);
  }
}

static int cmpU32(const void *a, const void *b) {
  uint32_t x = *(const uint32_t *)a, y = *(const uint32_t *)b;
  return (x > y) - (x < y);
}

// The measurement runs in its own FreeRTOS task, NOT in loop().
//
// libopus is built with VAR_ARRAYS: opus_encode() puts its MDCT/CELT scratch
// buffers on the *stack* (tens of KB). The Arduino loopTask stack is only 8 KB,
// so encoding from setup()/loop() trips the stack canary and panics ("Stack
// canary watchpoint triggered (loopTask)"). We give the encoder a generous
// dedicated stack and pin it to core 1 — core 0 is where BLE/relay would run,
// so this also matches the real runtime layout we care about.
static const uint32_t ENCODE_TASK_STACK = 48 * 1024;  // bytes; Opus is stack-hungry
static const BaseType_t ENCODE_TASK_CORE = 1;

// Complexity is the dominant CPU lever. For 16 kHz / 24 kbps voice, low
// complexity is usually transparent, so we sweep a range and print one row each
// to find the cheapest setting that still passes — in a single flash.
static const int COMPLEXITIES[] = {0, 1, 3, 5, 8, 10};
static const int NUM_COMPLEXITIES = sizeof(COMPLEXITIES) / sizeof(COMPLEXITIES[0]);

// Measure one complexity setting; fills avg/p95 (µs) and effective kbps via out-params.
// Returns false on an encoder error.
static bool measureComplexity(int complexity, uint32_t *outAvg, uint32_t *outP95,
                              float *outKbps) {
  int err = 0;
  OpusEncoder *enc = opus_encoder_create(SAMPLE_RATE, CHANNELS, APPLICATION, &err);
  if (err != OPUS_OK || enc == nullptr) {
    Serial.printf("  opus_encoder_create failed: %s\n", opus_strerror(err));
    return false;
  }
  opus_encoder_ctl(enc, OPUS_SET_BITRATE(BITRATE));
  opus_encoder_ctl(enc, OPUS_SET_SIGNAL(OPUS_SIGNAL_VOICE));
  opus_encoder_ctl(enc, OPUS_SET_COMPLEXITY(complexity));

  uint32_t phase = 0;
  long totalBytes = 0;

  for (int i = 0; i < WARMUP_FRAMES; i++) {
    fillFrame(pcm, phase); phase += FRAME_SAMPLES;
    opus_encode(enc, pcm, FRAME_SAMPLES, opusBuf, sizeof(opusBuf));
  }

  for (int i = 0; i < MEASURE_FRAMES; i++) {
    fillFrame(pcm, phase); phase += FRAME_SAMPLES;
    int64_t t0 = esp_timer_get_time();
    int n = opus_encode(enc, pcm, FRAME_SAMPLES, opusBuf, sizeof(opusBuf));
    int64_t t1 = esp_timer_get_time();
    if (n < 0) { Serial.printf("  encode error: %s\n", opus_strerror(n)); opus_encoder_destroy(enc); return false; }
    timings[i] = (uint32_t)(t1 - t0);
    totalBytes += n;
  }
  opus_encoder_destroy(enc);

  uint64_t sum = 0;
  for (int i = 0; i < MEASURE_FRAMES; i++) sum += timings[i];
  qsort(timings, MEASURE_FRAMES, sizeof(uint32_t), cmpU32);
  *outAvg  = (uint32_t)(sum / MEASURE_FRAMES);
  *outP95  = timings[(int)(MEASURE_FRAMES * 0.95)];
  *outKbps = ((float)totalBytes / MEASURE_FRAMES) * 8.0f / FRAME_MS;
  return true;
}

static void runSpike() {
  Serial.printf("real-time budget = %d us/frame; pass if avg<%d and p95<%d\n\n",
                REALTIME_BUDGET_US, PASS_AVG_US, PASS_P95_US);
  Serial.println(F("cplx |   avg us |   p95 us | core% | eff kbps | verdict"));
  Serial.println(F("-----+----------+----------+-------+----------+--------"));

  int bestPassing = -1;  // lowest-CPU complexity that still passes (we sweep low→high)
  for (int i = 0; i < NUM_COMPLEXITIES; i++) {
    int c = COMPLEXITIES[i];
    uint32_t avg = 0, p95 = 0;
    float kbps = 0.0f;
    if (!measureComplexity(c, &avg, &p95, &kbps)) continue;

    bool pass = (avg < PASS_AVG_US) && (p95 < PASS_P95_US);
    if (pass && bestPassing < 0) bestPassing = c;
    Serial.printf(" %3d | %8lu | %8lu | %4.0f%% | %8.1f | %s\n",
                  c, (unsigned long)avg, (unsigned long)p95,
                  100.0f * avg / REALTIME_BUDGET_US, kbps,
                  pass ? "PASS" : "fail");
  }

  Serial.println();
  if (bestPassing >= 0) {
    Serial.printf(">>> SPIKE 1: PASS — Opus is viable. Use complexity %d "
                  "(cheapest that clears headroom).\n", bestPassing);
  } else {
    Serial.println(F(">>> SPIKE 1: FAIL at every complexity — pivot to ADPCM/raw."));
  }
  Serial.printf("encode-task stack headroom: %u of %u bytes free\n",
                (unsigned)uxTaskGetStackHighWaterMark(nullptr),
                (unsigned)ENCODE_TASK_STACK);
  Serial.println(F("Record the chosen complexity + its avg/p95/core% on the scorecard."));
}

static void encodeTask(void *) {
  runSpike();
  vTaskDelete(nullptr);  // one-shot: done, remove self
}

void setup() {
  Serial.begin(115200);
  delay(1500);
  Serial.println();
  Serial.println(F("=== Spike 1: Opus encode load on ESP32-S3 ==="));
  Serial.printf("CPU freq: %d MHz, PSRAM: %s\n",
                getCpuFrequencyMhz(), psramFound() ? "yes" : "NO");
  Serial.printf("Config: %d Hz, %d ch, %d ms (%d samples), %d bps\n",
                SAMPLE_RATE, CHANNELS, FRAME_MS, FRAME_SAMPLES, BITRATE);

  xTaskCreatePinnedToCore(encodeTask, "opus_spike", ENCODE_TASK_STACK,
                          nullptr, 5, nullptr, ENCODE_TASK_CORE);
}

void loop() { delay(10000); }
