/*
 * OpenRecall — firmware configuration (XIAO ESP32S3 Sense, ESP-IDF).
 *
 * Single source of truth for the Phase-0-locked parameters. Values tagged [Spike N]
 * were measured on real hardware (firmware/README.md), not guessed. Must stay in
 * sync with the server's wire contract (openrecall_server/ingest/audio_packet.py).
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
/* Opus encode params. 24 kb/s VOIP at complexity 1 was the Spike-1 setting
 * (6.0 ms/frame, ~30% of core 1) — fine for the throwaway spike, but thin for
 * real speech: voice intelligibility suffers and the server-side ASR sees a
 * lossy signal. Bump to 32 kb/s (still well under the BLE notify ceiling) and
 * complexity 2 — a small core-1 cost for a real quality lift on the encoded
 * speech that downstream Whisper/Parakeet transcribe. The DC blocker above
 * also feeds a cleaner signal to the encoder, so the bits go to voice, not
 * to encoding a DC bias. MAX_OPUS_BYTES caps the per-frame size (fits §C.6 u8
 * len); 32 kb/s over 20 ms is 80 bytes worst case, well under 256. */
#define OPUS_BITRATE 32000
#define OPUS_COMPLEXITY 2  /* [Spike 1 was 1] small core-1 cost, real quality lift */
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
/* ---- Adaptive noise-floor VAD (runtime audio path; see vad_process_single_adaptive)
 *
 * The fixed VAD_ENERGY_THRESHOLD above drops quiet consonants/word-bodies at
 * wearable distance (speech ~1e5-2e5 at 10-15 cm, but the quiet parts dip to
 * ~2e4-4e4, below 5e4 -> never encoded -> chopped audio). The adaptive VAD
 * tracks the room's quiet-frame energy and sets the effective threshold per
 * frame:  thresh = max(FLOOR, noise_floor * MULTIPLIER).
 *
 *   VAD_ENERGY_THRESHOLD_FLOOR : absolute floor; effective threshold never
 *       drops below this (rejects room noise even in a silent room where the
 *       tracked floor would collapse). 1e4 sits ~2-3x above the ~3e3-5e3 room
 *       noise floor measured on this array.
 *   VAD_NOISE_MULTIPLIER       : threshold = noise_floor * MULTIPLIER. 3 means
 *       a frame must be ~3x the tracked noise to count as speech.
 *   VAD_NOISE_ALPHA_DOWN_Q16   : 0.02 in Q16 — fast floor decay (~1 s to settle
 *       when the room goes quiet). Update only on GAP frames.
 *   VAD_NOISE_ALPHA_UP_Q16      : 0.002 in Q16 — slow floor rise (~10 s), and
 *       only when e < 1.5*floor (a sudden loud burst does NOT raise the floor).
 *       Stops a stray syllable from poisoning the noise estimate. */
#define VAD_ENERGY_THRESHOLD_FLOOR 10000UL
#define VAD_NOISE_MULTIPLIER       3u
#define VAD_NOISE_ALPHA_DOWN_Q16   1311  /* 0.02 * 65536 */
#define VAD_NOISE_ALPHA_UP_Q16     131   /* 0.002 * 65536 */

/* ---- Input gain (pre-Opus, on the DC-blocked primary) ----
 * Fixed Q8 multiply: out = sat_int16(in * INPUT_GAIN_Q8 >> 8). The INMP441 has
 * no AGC and sits at ~1.4% full scale at wearable distance (amp ~450); x8
 * (2048 in Q8) lifts it to ~11% FS — a hotter signal for Opus and Whisper with
 * no clipping risk on normal speech. Saturates only on rare full-scale peaks.
 * Tunable here; runtime tunability is a Phase 3 concern. */
#define INPUT_GAIN_Q8 2048u   /* x8 in Q8 (8 << 8) */

/* ---- D1 button (GPIO2) gesture thresholds (spec 3.4) ----
 * Tunable. The classifier (button.c) is host-tested against these. The button
 * is on D1/GPIO2 (non-strapping; internal pull-up, press=LOW). Gestures:
 *   SHORT     -> mark moment (request_buffer 60 s)
 *   DOUBLE    -> toggle capture (audio_gate)
 *   LONG      -> capture_photo (snapshot now)
 *   VERY_LONG -> deep sleep (explicit only) */
#define BUTTON_GPIO            2     /* D1, non-strapping; internal pull-up, press=LOW */
#define BUTTON_DEBOUNCE_MS      20
#define BUTTON_DOUBLE_GAP_MS    400   /* second press within this of a short release = double */
#define BUTTON_LONG_MS          1000  /* hold >= this = long (snapshot); < this = a tap */
#define BUTTON_VERY_LONG_MS     6000  /* hold >= this = very-long (deep sleep) */

/* ---- D0 battery divider (GPIO1 = ADC1_CH0, 100k/100k, x2.0; spec 3.5) ----
 * Ported from the selftest test_battery.c (commit 77fda41). ADC1 is safe
 * alongside WiFi (ADC2 is NOT — WiFi steals it). 12 dB atten so a full 4.2 V
 * LiPo (-> 2.1 V at the pin) fits the ADC range. 64-sample oversample drops
 * the min/max for noise rejection. */
#define VBAT_GPIO        1     /* D0, ADC1 channel 0 */
#define VBAT_DIVIDER     2.0f   /* 100k/100k */
#define VBAT_MIN_MV      3000   /* 0% (single-LiPo floor) */
#define VBAT_FULL_MV     4100   /* 100% */
#define VBAT_MAX_MV      4250   /* above this is an error */
#define BATTERY_SAMPLE_INTERVAL_S  30
#define BATTERY_OVERSAMPLE           64

/* ---- BLE power: connection params + slow advertising (spec 3.2) ----
 * On connect we request a relaxed interval + slave latency so the phone's radio
 * can doze between audio bursts (the central may refuse — defaults then apply,
 * which is fine). Advertising only happens while disconnected, so a slow ~1 s
 * interval is purely a disconnected-idle saving with no speech-path cost.
 * Connection interval units = 1.25 ms; supervision timeout units = 10 ms. */
#define BLE_CONN_ITVL_MIN_UNITS   60    /*  75 ms (60 * 1.25) */
#define BLE_CONN_ITVL_MAX_UNITS  120    /* 150 ms (120 * 1.25) */
#define BLE_CONN_LATENCY           4    /* skip 4 intervals (slave latency) */
#define BLE_CONN_SUP_TIMEOUT_UNITS 600  /*  6 s (600 * 10) — > (1+lat)*itvl_max*2 */
#define BLE_ADV_ITVL_MIN_UNITS   1280   /*  800 ms (1280 * 0.625) */
#define BLE_ADV_ITVL_MAX_UNITS   1600   /* 1000 ms (1600 * 0.625) */

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
/* 10 * 20ms = 200ms chunks; source-side latency lever (spec §9 F1). */
#define C6_FRAMES_PER_CHUNK 10

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
#define OPENRECALL_SERVICE_UUID "6e9d0001-b5a3-4f6e-9b1a-7c2d5e8f0a10"
#define OPENRECALL_AUDIO_CHAR_UUID "6e9d0002-b5a3-4f6e-9b1a-7c2d5e8f0a10"   /* notify: §C.6 -> phone */
#define OPENRECALL_COMMAND_CHAR_UUID "6e9d0003-b5a3-4f6e-9b1a-7c2d5e8f0a10" /* write: signed §D in */
#define OPENRECALL_ACK_CHAR_UUID "6e9d0004-b5a3-4f6e-9b1a-7c2d5e8f0a10"     /* notify: command acks/status */

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
#define PRIMARY_CHANNEL 0 /* 1 = right is voice/primary, 0 = left is voice/primary */

/* ---- Server identity: the device verifies §D command signatures against this ----
 * Replace with your gateway's 32-byte Ed25519 public key (run_gateway.py prints it
 * as hex on startup). Placeholder all-zeros rejects everything until provisioned. */
static const uint8_t SERVER_ED25519_PUBKEY[32] = {0};

/* ---- §D command executors (P4a) ---- */
#define EXECUTOR_TASK_STACK 4096 /* core-0; cJSON parse + queue send only */
#define EXECUTOR_TASK_PRIO 5     /* same as the other app tasks */
#define EXECUTOR_TASK_CORE 0     /* with radio/drain, opposite audio encode */
#define EXECUTOR_QUEUE_DEPTH 8   /* SPSC; 8 in-flight commands is generous */

/* request_buffer bounds — mirror server _TYPE_SCHEMAS (defense-in-depth).
 * Seconds of retrospective audio to ship; used by executor_core.c's
 * replay_window + parse/validate. */
#define REQ_BUFFER_MIN_SECONDS 1
#define REQ_BUFFER_MAX_SECONDS 60
#define DRAIN_REPLAY_QUEUE_DEPTH 4 /* SPSC; replays serialize in the drain task */

/* ---- P4b video: OV2640 camera (XIAO ESP32S3 Sense, DVP) ----
 * Pinmap verified against the Seeed wiki + esp32-camera commit 9befde1
 * (CAMERA_MODEL_XIAO_ESP32S3). NO collision with I2S mics (4/5/6),
 * SD SPI (7/8/9/21), or strapping (0/3/45/46). GPIO21 is the camera
 * LED-flash AND the SD CS — leave the LED unused (CAM_PIN_LED = -1). */
#define CAM_PIN_XCLK    10
#define CAM_PIN_SIOD    40   /* SCCB SDA */
#define CAM_PIN_SIOC    39   /* SCCB SCL */
#define CAM_PIN_D7      48
#define CAM_PIN_D6      11
#define CAM_PIN_D5      12
#define CAM_PIN_D4      14
#define CAM_PIN_D3      16
#define CAM_PIN_D2      18
#define CAM_PIN_D1      17
#define CAM_PIN_D0      15
#define CAM_PIN_VSYNC   38
#define CAM_PIN_HREF    47
#define CAM_PIN_PCLK    13
#define CAM_PIN_PWDN    -1
#define CAM_PIN_RESET   -1
#define CAM_PIN_LED     -1   /* GPIO21 is SD CS — do NOT drive the camera LED */
#define CAM_XCLK_FREQ_HZ 20000000
#define VIDEO_WIDTH    640   /* VGA */
#define VIDEO_HEIGHT   480
#define VIDEO_FPS      10   /* target; Spike-2 measured ~34 fps headroom at VGA */
#define VIDEO_JPEG_QUALITY 12   /* 0-63, lower = better; OV2640 quantization */

/* ---- P4b video: ambient snapshot cadence ----
 * Default OFF (0): snapshots are the single biggest battery drain (OV2640 +
 * SD write + JPEG encode on every interval), and the camera/PSRAM contention
 * with the audio ring was a real-hardware e_pri spike (commit ba327dc). The
 * server can re-enable per-session via set_snapshot_interval, and the button
 * LONG gesture fires a one-shot capture on demand. Re-enable by default only
 * once idle current is characterized on hardware (spec 3.6). */
#define SNAPSHOT_INTERVAL_S 0           /* default; 0 = off. Server overrides via set_snapshot_interval */
#define SNAPSHOT_INTERVAL_MIN 0         /* 0 = off */
#define SNAPSHOT_INTERVAL_MAX 600       /* mirror server _TYPE_SCHEMAS */

/* ---- P4b video: bounded SoftAP transfer (spec D5 / §3.2) ---- */
#define WIFI_TRANSFER_WINDOW_S 600      /* hard max SoftAP up-time; protects battery */
#define SOFTAP_SSID_PREFIX "OpenRecall-" /* SSID = prefix + boot_id (e.g. OpenRecall-7) */
#define SOFTAP_CHANNEL 1
#define TRANSFER_HTTP_PORT 80
#define TRANSFER_TASK_STACK 6144        /* esp_http_server + fatfs reads on core 0 */
#define TRANSFER_TASK_PRIO 4            /* below audio/BLE real-time */

/* ---- P4b video: boot_id (NVS counter per activate) ---- */
#define BOOT_NVS_NAMESPACE "sense_boot"
#define BOOT_NVS_KEY "boot_id"

/* ---- P4b video: SD layout ---- */
#define SD_MOUNT_POINT "/sdcard"
#define SD_SNAPSHOT_DIR "/sdcard/snapshots"
#define SD_VIDEO_DIR "/sdcard/video"
#define SD_MANIFEST "/sdcard/manifest.txt"
