# Sense Firmware — Dual IENMP441 Voice-Isolating Microphone Array

> **Goal:** replace the single onboard PDM microphone with a two-microphone I2S array (IENMP441 / INMP441-class MEMS mics on a shared I2S bus) and add on-device differential noise cancellation plus a dual-channel voice-activity detector, so the device streams clean mono speech to the server with minimal junk and minimal interruptions in noisy environments. The uplink contract (mono 16 kHz Opus 24 kbps over §C.6) is unchanged; `ble_drain`, `c6_packet`, and `ble_link` are untouched.

## 1. Context

The Sense wearable firmware (`firmware/sense_sensor/`, ESP-IDF v5.1.6, ESP32-S3 / XIAO ESP32S3 Sense, NimBLE 1.6) currently captures audio from a single PDM MEMS mic on GPIO 42 (CLK) / GPIO 41 (DIN) via the per-peripheral I2S PDM RX API (`driver/i2s_pdm.h`). Each 20 ms frame (320 samples @ 16 kHz, mono, 16-bit) is gated by a single-channel **energy-threshold VAD** (`vad.c`, `VAD_ENERGY_THRESHOLD = 2_000_000`), encoded to Opus 24 kbps VOIP, pushed into a 60 s PSRAM ring buffer, drained in MTU-sized §C.6 packets over BLE, and relayed to the server.

Two problems motivate this slice:

1. **No noise rejection.** A single omnidirectional mic captures voice and ambient noise identically. In noisy environments the energy VAD fires on loud ambient (encoding junk) and cuts quiet real speech (interruptions), and the Opus stream carries noise to the server's transcription pipeline, hurting accuracy.
2. **Single-channel VAD is brittle.** Energy thresholding cannot distinguish "loud ambient" from "quiet voice," which is the direct cause of the "junk data" and "minimal interruptions" the user wants fixed.

A dual-mic array resolves both: two IENMP441 mics on a shared I2S bus (shared SCK + WS + SD, distinguished by L/R channel-select) give a stereo stream where the **primary** mic (front, facing the wearer's voice) and the **reference** mic (back, facing ambient) hear correlated noise in common but voice only on the primary. On-device **adaptive differential cancellation** subtracts the correlated noise; a **dual-channel ratio VAD** declares speech only when the primary is louder than the reference by a margin. The result is clean mono PCM fed into the existing Opus → ring → §C.6 → BLE path.

**Hardware change.** The onboard PDM mic (GPIO 41/42) is removed and replaced with two external IENMP441 I2S MEMS microphones wired to a shared bus. The user's initially requested pins (GPIO 7/8/9) collide exactly with the microSD SPI bus (`config.h:67-69`: SCK=7, MISO=8, MOSI=9); to keep microSD available for future EOD-video capture, the mic is moved to free GPIOs (see §4).

## 2. Architecture

```
I2S STD stereo RX (16 kHz, 16-bit, L+R) ── SCK/WS/SD on GPIO 4/5/6 ──
   │  audio_capture_read_stereo(primary[320], reference[320])   per 20 ms, core 1
   ▼
mic_dsp_process(&dsp, primary, reference, out_mono, adapt_now)
   │   y[n] = primary[n] − Σ w[k]·reference[n−k]    (NLMS, fixed-point Q15)
   │   w adapts ONLY when adapt_now (noise-only frame); frozen during speech
   ▼
vad_process_dual(&vad, primary, reference, n)
   │   e_pri, e_ref; ratio = e_pri/(e_ref+ε); speech iff e_pri>ETH AND ratio>RATIO
   │   → C6_SPEECH / C6_HANGOVER / C6_GAP_MARKER   (+ hangover state machine)
   ▼
opus_stream_encode(out_mono)        ── unchanged, mono 24 kbps
   ▼
ring_buffer_push / ble_drain / c6_packet / ble_link   ── UNCHANGED
```

Module boundaries (each independently understandable and host-testable):

1. **`audio_capture`** — pure I2S transport. Reads one 20 ms **stereo** frame from I2S std RX, deinterleaves into `primary[320]` + `reference[320]` according to `PRIMARY_CHANNEL`. No DSP. Owns the `i2s_chan_handle_t` RX channel.
2. **`mic_dsp`** (new) — adaptive differential noise cancellation. Holds the NLMS filter state in internal SRAM. Pure function on int16 arrays: `mic_dsp_process(&dsp, primary, reference, out, adapt_now)`. No ESP/I2S dependency → host-testable.
3. **`vad`** — upgraded from single-channel energy to **dual-channel ratio VAD**. Pure function on int16 arrays: `vad_process_dual(&vad, primary, reference, n)` → same `C6_SPEECH/HANGOVER/GAP_MARKER` vocabulary the drainer already consumes. Host-testable.
4. **`opus_stream` / `ring_buffer` / `ble_drain` / `c6_packet` / `ble_link`** — **unchanged.** They only ever see mono Opus bytes; the wire contract, ring format, §C.6 packet format, and BLE GATT layout are identical.

`audio_task` (core 1, stack 32 KB, prio 5) loop becomes, per 20 ms frame:

```c
audio_capture_read_stereo(pri, ref);          // was: audio_capture_read_frame(pcm)
vad_state_t s = vad_process_dual(&vad, pri, ref, FRAME_SAMPLES);
bool adapt_now = (s == C6_GAP_MARKER);        // adapt only on noise-only frames
mic_dsp_process(&dsp, pri, ref, mono, adapt_now);
if (s == C6_SPEECH || s == C6_HANGOVER) {
    opus_stream_encode(mono, opus_buf, …);
    ring_buffer_push(state, rel_ts_ms, opus_buf, n);
} else {
    ring_buffer_push(state, rel_ts_ms, NULL, C6_GAP_MARKER);
}
rel_ts_ms += FRAME_MS;
```

The VAD runs **before** the DSP so the same decision gates both the Opus encode (unchanged behavior) and the NLMS adaptation (new). No cross-task state; everything is local to `audio_task`.

## 3. Component details

### 3.1 `audio_capture` — I2S standard stereo RX

Replace `driver/i2s_pdm.h` with `driver/i2s_std.h`. New init:

```c
i2s_chan_handle_t s_rx_chan;
i2s_chan_config_t chan_cfg = I2S_CHANNEL_DEFAULT_CONFIG(I2S_NUM_AUTO, I2S_ROLE_MASTER);
chan_cfg.dma_desc_num = 6;
chan_cfg.dma_frame_num = FRAME_SAMPLES;          // 320 per channel
i2s_new_channel(&chan_cfg, NULL, &s_rx_chan);

i2s_std_clk_config_t clk = I2S_STD_CLK_DEFAULT_CONFIG(SAMPLE_RATE);   // 16000
i2s_std_gpio_config_t gpio = {
    .sck = I2S_BCK_GPIO, .ws = I2S_WS_GPIO, .dout = -1, .din = I2S_DATA_GPIO,
    .invert_flags = { .mclk_inv = false, .bclk_inv = false, .ws_inv = false },
};
i2s_std_slot_config_t slot = I2S_STD_PHILIPS_DEFAULT_CONFIG(I2S_DATA_BIT_WIDTH_16BIT,
                                                            I2S_SLOT_MODE_STEREO);
// both slots enabled (L+R); slot_mask = I2S_STD_SLOT_LEFT | I2S_STD_SLOT_RIGHT
i2s_std_rx_config_t rx = { .clk_cfg = &clk, .slot_cfg = &slot, .gpio_cfg = &gpio };
i2s_channel_init_std_rx_mode(s_rx_chan, &rx);   // replaces the old i2s_channel_init_pdm_rx_mode
i2s_channel_enable(s_rx_chan);
```

Read: `audio_capture_read_stereo(int16_t *primary, int16_t *reference)` reads `FRAME_SAMPLES * 2` samples (1280 bytes) of interleaved L/R via `i2s_channel_read`, then deinterleaves: if `PRIMARY_CHANNEL == 0`, `primary[i] = buf[2i]`, `reference[i] = buf[2i+1]`; else swapped. Returns `ESP_OK` only on a full frame; on a short read logs and returns `ESP_ERR_INVALID_SIZE` (caller skips the frame but still advances `rel_ts_ms`).

### 3.2 `mic_dsp` — NLMS adaptive differential cancellation

Classic Widrow adaptive noise cancellation, int32 Q15 fixed-point (ESP32-S3 has an FPU, but Q15 is bit-stable and cheaper; avoids float denormals). State:

```c
typedef struct {
    int32_t w[DSP_NLMS_TAPS];        // Q15 filter coefficients (reference→primary noise path)
    int32_t delay[DSP_NLMS_TAPS];    // reference sample delay ring
    int      didx;                    // ring write index
} mic_dsp_t;
```

Per sample `n` (over 320 samples):

```
y[n]      = primary[n] − Σ_{k=0..L-1} w[k]·delay[k]        // Q15→Q0, saturate to int16
out[n]    = sat_int16(y[n])
if (adapt_now) {
    pow = Σ delay[k]²                                  // Q30 ref power
    mu_n = DSP_NLMS_STEP_Q15 / (pow + DSP_NLMS_EPS_Q30)
    e    = primary[n] − y[n]                            // error = residual after subtraction
    w[k] += (mu_n * e) * delay[k]   >> 15              // NLMS update, per tap
    // plus leakage: w[k] -= (w[k] * DSP_NLMS_LEAK_Q15) >> 15
}
push reference[n] into delay ring
```

**Adaptation gate.** `adapt_now` is `true` only on noise-only frames (`vad_state == C6_GAP_MARKER`). During speech/hangover the filter is **frozen** — it holds the noise path it learned during the last noise-only segment and subtracts only that. Because the reference mic faces away from the voice, it hears ambient noise but little voice, so the learned filter cancels noise without touching voice.

Tunables (`config.h`, all marked "TUNE ON HARDWARE" — values below are initial defaults to be calibrated in the §6 hardware smoke step): `DSP_NLMS_TAPS = 32` (2 ms at 16 kHz — covers the front/back path mismatch), `DSP_NLMS_STEP_Q15 = 6553` (≈0.2 in Q15), `DSP_NLMS_LEAK_Q15 = 1` (minimal leakage, ≈3e-5), `DSP_NLMS_EPS_Q30 = 512` (regularization to avoid div-by-zero on silent reference).

Cost: ~32 MACs/sample × 32 k/s ≈ 1 M MAC/s on core 1 — negligible relative to the Opus encode. State is ~256 bytes in internal SRAM (not PSRAM) so the hot path never touches SPIRAM.

### 3.3 `vad` — dual-channel ratio VAD

Replaces the energy-only `vad_process`. New signature `vad_state_t vad_process_dual(vad_t *v, const int16_t *primary, const int16_t *reference, size_t n)`:

```
e_pri = Σ primary[i]²  / n
e_ref = Σ reference[i]² / n
ratio = e_pri / (e_ref + ε)                          // ε = 1 to avoid div0
speech = (e_pri > VAD_ENERGY_THRESHOLD) && (ratio > VAD_RATIO_THRESHOLD)
```

- `VAD_ENERGY_THRESHOLD` stays (default 2_000_000, retune). Guards against encoding near-silence.
- `VAD_RATIO_THRESHOLD` (default 4×, "TUNE ON HARDWARE"). Loud ambient that lands similarly on both mics (ratio ≈ 1) is **not** speech → no encode, no junk. Quiet real voice that is louder on the primary than the reference still passes.
- Hangover state machine unchanged (`VAD_HANGOVER_FRAMES = 30` / 600 ms): a `C6_SPEECH` frame re-arms hangover; following frames emit `C6_HANGOVER` until the counter drains, then `C6_GAP_MARKER`.
- Preroll (`VAD_PREROLL_FRAMES = 15` / 300 ms) is handled by the drainer as today — no change.

**Graceful fallback.** If the reference mic is dead/disconnected, `e_ref ≈ 0` ⇒ `ratio → ∞` ⇒ the ratio test is satisfied whenever `e_pri > VAD_ENERGY_THRESHOLD`, i.e. pure energy VAD. Simultaneously `mic_dsp` output ≈ primary (the filter converges to ~0 on a silent reference). The device keeps working as a single-mic device with no special-casing.

### 3.4 `config.h` changes

| Change | Detail |
|---|---|
| Remove | `PDM_CLK_GPIO`, `PDM_DIN_GPIO` |
| Add | `I2S_BCK_GPIO 4`, `I2S_WS_GPIO 5`, `I2S_DATA_GPIO 6` |
| Add | `PRIMARY_CHANNEL 0` (0 = left/voice, 1 = right/voice) |
| Add | `DSP_NLMS_TAPS 32`, `DSP_NLMS_STEP_Q15 6553`, `DSP_NLMS_LEAK_Q15 1`, `DSP_NLMS_EPS_Q30 512` |
| Add | `VAD_RATIO_THRESHOLD 4` |
| Keep | `SAMPLE_RATE 16000`, `CHANNELS 1`, `FRAME_SAMPLES 320`, `FRAME_BYTES 640` (Opus still mono), `VAD_*` hangover/preroll/energy |
| Update | `CMakeLists.txt` SRCS adds `mic_dsp.c`; `REQUIRES` unchanged (`driver` still covers I2S std in IDF 5.1.6) |

Log strings in `audio_capture.c` and `sense_sensor.c` ("PDM RX up: … mono" → "I2S STD RX up: … stereo, primary=ch%d").

### 3.5 L/R channel-select wiring

Each IENMP441 has an L/R channel-select pin. Tie mic #1's L/R to **GND** (outputs on the left slot) and mic #2's L/R to **VDD** (right slot). `PRIMARY_CHANNEL` in `config.h` selects which deinterleaved channel is treated as the voice/primary mic, so flipping primary↔reference is a one-line config change + reflash, no rewiring. Physical placement: mic #1 (primary) faces the wearer's mouth/body; mic #2 (reference) faces outward to ambient.

## 4. Pin map

GPIOs are routed through the ESP32-S3 GPIO matrix, so any free, non-strapping pin works for I2S std. Chosen to avoid the microSD SPI bus (7/8/9), strapping pins (0/3/45/46), flash/PSRAM (26–32), and the camera interface:

| Function | Pin | Silkscreen | Notes |
|---|---|---|---|
| I2S BCK (SCK) | GPIO 4 | D3 | shared bus |
| I2S WS (LRCLK) | GPIO 5 | D4 | shared bus |
| I2S SD (DATA) | GPIO 6 | D5 | shared bus, both mics |
| Mic #1 L/R | GND | — | → left channel (primary) |
| Mic #2 L/R | VDD (3V3) | — | → right channel (reference) |
| Mic VDD | 3V3 | 3V3 | both mics |
| Mic GND | GND | GND | both mics |

microSD (GPIO 7/8/9/21) is left intact and available for future EOD-video capture. The old PDM pins (41/42) are freed.

## 5. Error handling & robustness

- **I2S init failure** — logged `ESP_LOGE` and treated as fatal at boot (same as today's PDM init).
- **Short frame read** — `audio_capture_read_stereo` returns `ESP_ERR_INVALID_SIZE`; `audio_task` skips the DSP/encode for that frame but still advances `rel_ts_ms += FRAME_MS` so the ring stays contiguous and the drainer's `chunk_seq`/`rel_ts` monotonicity holds.
- **Sample saturation** — `sat_int16` clamp on the DSP output; Opus input is clean int16.
- **Adaptive filter runaway** — prevented by (a) freeze-during-speech, (b) normalized step (NLMS divides by reference power + ε), (c) leakage term. Coefficients are bounded by construction.
- **Reference mic failure** — graceful fallback to energy-only VAD + bypass cancellation (§3.3).
- **PSRAM discipline** — only the ring buffer uses PSRAM (unchanged); per-frame `primary/reference/mono` buffers are stack/internal-SRAM `static int16_t` in `audio_task`; `mic_dsp_t` state is internal SRAM. The `CONFIG_SPIRAM_USE_CAPS_ALLOC=y` workaround for the NimBLE `xQueueSemaphoreTake uxItemSize==0` assert is preserved untouched.

## 6. Testing

Following the existing host contract-test pattern (`firmware/sense_sensor/test/` has `c6_packet`, `vad`, `provisioning` tests with no ESP deps, run via the host CMake target):

- **`test/mic_dsp_test.c`**
  - *Cancellation:* primary = voice-tone (e.g. 800 Hz) + correlated-noise (sweep); reference = the same correlated-noise only. After a noise-only adaptation warm-up, assert the noise component in `out` is attenuated by ≥ 6 dB and the voice-tone is preserved (within 1 dB).
  - *Freeze-during-speech:* run a noise-only adapt phase, snapshot `w`, then feed a speech-flagged frame with `adapt_now=false`, assert `w` is byte-identical.
  - *Reference-silent fallback:* reference all zeros, assert `out ≈ primary` (≤ 1 LSB diff) and no NaN/overflow.
  - *Stability:* long run (60 s of synthetic data) with alternating noise/speech, assert `w` stays bounded (max |w| < saturating threshold).

- **`test/vad_dual_test.c`**
  - *Loud correlated ambient:* primary ≈ reference (ratio ≈ 1), both above energy threshold → `C6_GAP_MARKER` (not speech). This is the junk-rejection guarantee.
  - *Quiet voice on primary only:* `e_pri` below the old energy-only threshold but `ratio > VAD_RATIO_THRESHOLD` → still `C6_SPEECH` (if `e_pri > VAD_ENERGY_THRESHOLD`); confirm the ratio test lifts quiet-voice detection relative to a pure energy VAD while the energy floor still blocks silence.
  - *Hangover:* a speech frame followed by N non-speech frames emits `C6_HANGOVER` for 600 ms then `C6_GAP_MARKER`.
  - *Reference-dead fallback:* `e_ref ≈ 0`, loud primary → `C6_SPEECH`.

- **Hardware smoke** (not host-testable): on-device capture, log per-frame `e_pri/e_ref/ratio` and `out` energy for 5 s of silence, 5 s of ambient, 5 s of speech; confirm the ratio separates speech from ambient and the DSP output energy drops during ambient. This is the "TUNE ON HARDWARE" calibration step that finalizes `VAD_RATIO_THRESHOLD`, `DSP_NLMS_STEP_Q15`, and `DSP_NLMS_TAPS`.

## 7. Out of scope

- **No change to the uplink contract.** Mono Opus 24 kbps over §C.6 is preserved. Stereo streaming, server-side noise suppression, and a new server DSP stage are explicitly out — the device ships clean mono.
- **No esp-nsk / ESP-SR.** Evaluated and deferred: heavy dependency, uncertain IDF 5.1.6 compatibility, and risk to the working NimBLE/PSRAM setup. The NLMS + ratio-VAD path delivers the user's goals at a fraction of the cost; esp-nsk remains a future upgrade path if hardware tuning shows it's needed.
- **No on-device wake word / STT / inference.** The device remains a pure audio pump (per README); dual-mic is a capture-quality improvement only.
- **No microSD changes.** SD stays on 7/8/9/21, untouched.
- **No BLE / provisioning / command-path changes.**