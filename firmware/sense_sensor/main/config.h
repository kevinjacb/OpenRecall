/*
 * Sense AI Sensor — firmware configuration (XIAO ESP32S3 Sense, ESP-IDF).
 *
 * Single source of truth for the Phase-0-locked parameters. Values tagged [Spike N]
 * were measured on real hardware (firmware/README.md), not guessed. Must stay in
 * sync with the server's wire contract (sense_server/ingest/audio_packet.py).
 *
 * Pure C / host-compilable (only <stdint.h>): used by both the firmware and the
 * host-side §C.6 contract test. Compile-time constants are macros/enums so they are
 * valid C constant expressions (array sizes, other macros).
 */
#pragma once

#include <stdint.h>

/* ---- Audio (must match the server's Opus decode + §C.6 expectations) ---- */
#define SAMPLE_RATE 16000 /* Hz, mono */
#define CHANNELS 1
#define FRAME_MS 20                                   /* one Opus frame == 20 ms */
#define FRAME_SAMPLES (SAMPLE_RATE * FRAME_MS / 1000) /* 320 */
#define FRAME_BYTES (FRAME_SAMPLES * 2)               /* 640 (int16) */
#define OPUS_BITRATE 24000
#define OPUS_COMPLEXITY 1  /* [Spike 1] 6.0 ms/frame, ~30% of core 1 */
#define MAX_OPUS_BYTES 256 /* per-frame encoded cap (fits §C.6 u8 len) */

/* ---- Ring buffer (retrospective capture: "remember that") ---- */
#define RING_SECONDS 60                              /* PSRAM-backed history window */
#define RING_FRAMES (RING_SECONDS * 1000 / FRAME_MS) /* 3000 frames */

/* ---- Software VAD (gate 24/7 audio; tag silence as gap markers) ---- */
#define VAD_HANGOVER_MS 600                              /* keep emitting after speech ends */
#define VAD_HANGOVER_FRAMES (VAD_HANGOVER_MS / FRAME_MS) /* 30 */
#define VAD_PREROLL_MS 300                               /* replay this much history on onset */
#define VAD_PREROLL_FRAMES (VAD_PREROLL_MS / FRAME_MS)   /* 15 */
/* Per-sample mean-square energy gate on the primary (mouth/voice) channel.
 * Units: the VAD compares per-frame per-sample ms (sum_of_squares / 320) to
 * this value; the per-second cal log prints the sum over 16,000 samples, so
 * cal_e_pri / 16000 is the comparable per-sample ms. Empirically measured on
 * this IENMP441 array at 16-bit: room noise floor ~3e3-5e3 (RMS amp ~60-70),
 * conversational speech at 10-15 cm ~1e5-2e5 (amp ~300-450) with peaks ~3e5.
 * 5e4 (amp ~224) sits ~10x above the noise floor and ~2-4x below speech, so it
 * captures the FULL utterance (not just the loud peaks) while rejecting room
 * noise. NOTE: an earlier 2e5 value was calibrated for speech ~5e5-1e6
 * (amp ~700-1000) which this array does not produce at wearable distance; at
 * 2e5 the VAD fired only on speech peaks (~30% of frames voiced during
 * continuous speech), dropping the quieter consonants/word-bodies ASR needs
 * -> chopped audio -> poor transcription at 10-15 cm while close-up speech
 * (above 2e5) transcribed fine. At arm's length the INMP441 signal collapses
 * to amp ~150-230 (ms ~2e4-5e4), indistinguishable from a louder room — no
 * threshold can discriminate that; the device must be at wearable distance.
 * The audio path runs SINGLE-mic energy VAD (vad_process_single); the ratio
 * gate below is retained for the dual-mic host test but is NOT used at runtime
 * (two omnidirectional mics with insufficient shadowing give ratio ~1, which
 * would reject the voice). Exposed as a runtime arg so the VAD logic is
 * host-testable. */
#define VAD_ENERGY_THRESHOLD 50000UL
/* Dual-channel ratio gate (NOT on the runtime audio path; kept for the
 * dual-mic host test and for future use IF real acoustic shadowing ever gives
 * ratio >> 1). Would require primary energy to exceed reference energy by this
 * factor to count as speech. */
#define VAD_RATIO_THRESHOLD 4u

/* ---- Dual-mic DSP: NLMS adaptive differential noise cancellation ----
 * Pure fixed-point (int32 Q15). The reference mic (back, ambient) drives an
 * adaptive FIR that models the noise path to the primary (front, voice) mic;
 * the filtered reference is subtracted from the primary. The filter adapts
 * ONLY on noise-only frames (VAD says C6_GAP_MARKER) and is frozen during
 * speech so it cancels noise, not voice. All values TUNE ON HARDWARE. */
#define DSP_NLMS_TAPS 32       /* filter order; 2 ms at 16 kHz */
#define DSP_NLMS_STEP_Q15 6553 /* normalized step ~0.2 in Q15 */
#define DSP_NLMS_LEAK_Q15 1    /* leakage (~3e-5) to bound the filter */
#define DSP_NLMS_EPS 512       /* ref-power regularization (same scale as ||x||^2) */

/* ---- §C.6 audio packet wire format (mirror of audio_packet.py) ---- */
#define C6_VERSION 1
#define C6_HEADER_LEN 12
#define C6_FLAG_HISTORICAL 0x01
#define C6_FLAG_LAST_OF_REQ 0x02
/* 50 frames == 1 s of audio per BLE notify burst (BLE overhead vs latency balance). */
#define C6_FRAMES_PER_CHUNK 50

enum c6_packet_type
{
    C6_LIVE = 0,
    C6_MEMORY_CHUNK = 1,
    C6_HISTORICAL = 2
};
enum c6_vad_state
{
    C6_GAP_MARKER = 0,
    C6_SPEECH = 1,
    C6_PREROLL = 2,
    C6_HANGOVER = 3
};

/* ---- BLE GATT (separated planes; the phone relay bridges these to the server) ---- */
#define SENSE_SERVICE_UUID "6e9d0001-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define SENSE_AUDIO_CHAR_UUID "6e9d0002-b5a3-4f6e-9b1a-7c2d5e8f0a10"   /* notify: §C.6 -> phone */
#define SENSE_COMMAND_CHAR_UUID "6e9d0003-b5a3-4f6e-9b1a-7c2d5e8f0a10" /* write: signed §D in */
#define SENSE_ACK_CHAR_UUID "6e9d0004-b5a3-4f6e-9b1a-7c2d5e8f0a10"     /* notify: command acks/status */

/* ---- Provisioning service (separate GATT service; Android pairing flow) ---- */
#define PROV_SERVICE_UUID "6e9d0010-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define PROV_STATE_CHAR_UUID "6e9d0011-b5a3-4f6e-9b1a-7c2d5e8f0a10" /* read+notify: 0=unprov,1=prov */
#define PROV_KEY_CHAR_UUID "6e9d0012-b5a3-4f6e-9b1a-7c2d5e8f0a10"   /* write: 32B server pubkey */
#define PROV_RESET_CHAR_UUID "6e9d0013-b5a3-4f6e-9b1a-7c2d5e8f0a10" /* write: factory-reset magic */
#define PROV_NVS_NAMESPACE "sense_prov"
#define PROV_NVS_KEY "srvkey"
#define PROV_NVS_PROVISIONED "provd"
#define PROV_FACTORY_RESET_MAGIC 0xA5A5A5A5u

/* ---- microSD over SPI ([Spike 2] pins verified; throughput plateaus at 20 MHz) ---- */
#define SD_PIN_SCK 7
#define SD_PIN_MISO 8
#define SD_PIN_MOSI 9
#define SD_PIN_CS 21
#define SD_SPI_HZ 20000000

/* ---- Dual IENMP441 I2S microphone array (shared bus) ----
 * Two I2S MEMS mics share SCK + WS + SD; each mic's L/R channel-select pin
 * ties one to the left slot (GND) and the other to the right slot (VDD).
 * PRIMARY_CHANNEL picks which deinterleaved channel is the voice/primary mic
 * (front, faces wearer) vs the noise reference (back, faces ambient). Flip by
 * changing this constant — no rewiring. Pins chosen to avoid the microSD SPI
 * bus (GPIO 7/8/9/21), strapping pins (0/3/45/46), and flash/PSRAM (26-32). */
#define I2S_BCK_GPIO 5  /* SCK  (D3) */
#define I2S_WS_GPIO 6   /* LRCLK (D4) */
#define I2S_DATA_GPIO 4 /* SD   (D5) */
/* Left mic (L/R tied to GND) faces the wearer's mouth -> voice/primary.
 * Right mic (L/R tied to VDD) faces away / is shadowed -> ambient ref.
 * The reference is currently unused for DSP (single-mic energy VAD; see
 * audio_task) but is still captured for the per-second cal log, so keep it on
 * the away mic. Flip to 1 only if you re-mount the mouth mic on the right
 * (VDD) channel. */
#define PRIMARY_CHANNEL 1 /* 1 = right is voice/primary, 0 = left is voice/primary */

/* ---- Server identity: the device verifies §D command signatures against this ----
 * Replace with your gateway's 32-byte Ed25519 public key (run_gateway.py prints it
 * as hex on startup). Placeholder all-zeros rejects everything until provisioned. */
static const uint8_t SERVER_ED25519_PUBKEY[32] = {0};

/* ---- §D command executors (P4a) ---- */
#define EXECUTOR_TASK_STACK      4096   /* core-0; cJSON parse + queue send only */
#define EXECUTOR_TASK_PRIO       5      /* same as the other app tasks */
#define EXECUTOR_TASK_CORE       0      /* with radio/drain, opposite audio encode */
#define EXECUTOR_QUEUE_DEPTH     8      /* SPSC; 8 in-flight commands is generous */

/* request_buffer bounds — mirror server _TYPE_SCHEMAS (defense-in-depth).
 * Seconds of retrospective audio to ship; used by executor_core.c's
 * replay_window + parse/validate. */
#define REQ_BUFFER_MIN_SECONDS 1
#define REQ_BUFFER_MAX_SECONDS 60
