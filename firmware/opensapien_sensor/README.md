# OpenSapien — firmware (ESP-IDF)

Product firmware for the wearable (XIAO ESP32S3 Sense). **ESP-IDF**, not Arduino —
this is the efficient foundation: NimBLE (lighter than Bluedroid), DMA I2S capture,
real FreeRTOS core control, mbedTLS for Ed25519, `-O2`. The Phase-0 spike sketches in
`../spike*/` were throwaway; this is the real thing, with their measured parameters
(`main/config.h`) baked in.

## The wearable is dumb (by design)

It does **only**: capture dual-mic audio → noise-cancel + VAD-gate → Opus-encode →
PSRAM ring buffer → stream as **§C.6** over BLE; and receive **§D** signed commands →
verify against the provisioned server key → execute → ack. No wake word, STT,
embeddings, or LLM. The dual-mic array and on-board NLMS noise cancellation are a
**capture-quality** improvement — the device still ships clean **mono** Opus over the
unchanged §C.6 uplink.

### Tier split (important)

The device does **not** speak §E session framing — the **phone relay** does:

```
 DEVICE (this firmware)            PHONE RELAY                     SERVER
 ─────────────────────            ───────────                     ──────
 §C.6 audio  ──BLE notify──▶  forward verbatim   ──WS binary──▶  ingest
 verify+exec §D  ◀─BLE write─  forward command   ◀──WS──────────  signed §D
 command ack  ──BLE notify──▶  wrap as §E         ──WS command_ack──▶ dispatcher
                              (owns hello/bye, acks, transcripts)
```

So the device speaks **§C.6 (audio) + §D (commands)** over BLE; the phone owns **§E**.
The server's reference `DeviceClient` simulates *device + phone combined*; here they
are split into the two real tiers.

## Build & flash

```bash
# One-shot: clones ESP-IDF v5.1.6 to ~/esp/esp-idf-v5.1.6/, installs the S3
# toolchain + Python env, resets the 6.0.1-derived project state, and
# re-runs set-target + reconfigure. Side-by-side with the v6.0.1 install
# at /Users/kevin/.espressif/v6.0.1/ — neither install touches the other.
./scripts/install_idf_5.1.6.sh

# Then in any new shell:
. ~/esp/esp-idf-v5.1.6/export.sh
idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor
```

We pin to **ESP-IDF v5.1.6** (NimBLE 1.6) — the last 5.x line that's been
heavily field-tested on Xtensa LX7 + S3. IDF 6.0.1 (NimBLE 1.7) hits a
`xQueueSemaphoreTake uxItemSize == 0` assert on the first BLE connect that
isn't yet fixed upstream. Once the bring-up is live end-to-end we can revisit
IDF 6 with more time + a real debugger.

Provision the server key first: copy the hex `run_gateway.py` prints on startup into
`SERVER_ED25519_PUBKEY` in `main/config.h` (the device verifies command signatures
against it).

## Host contract tests (no hardware)

Dependency-free modules are unit-tested on the dev host against the **server's**
encoder, so the wire format can't drift between firmware and server:

```bash
cd test && make
```

`test_c6_packet.c` checks the §C.6 writer byte-for-byte against a golden vector
produced by the server's `AudioPacket.encode()`.

## Build status

Builds clean against **ESP-IDF v5.1.6** for `esp32s3` (`idf.py build`). Boots
clean, audio path runs at 50 fps (frames/voiced/gap logged every second;
`opus_bytes/s > 0` once a phone relay subscribes). The first BLE
`GAP connect` event from the Android app processes cleanly on NimBLE 1.6
(no `xQueueSemaphoreTake uxItemSize == 0` assert). Server
`data/events.db` `capture_events` table accumulates rows end-to-end via
`server/scripts/run_device_sim.py` (Python simulator stands in for
device+phone combined for back-end bring-up).

## Module roadmap

| Module | Status | Notes |
|---|---|---|
| `c6_packet` | ✅ host-tested + builds | §C.6 writer, byte-matches server encoder |
| `config` | ✅ builds | Spike-locked params + BLE UUIDs + pins |
| `audio_capture` | ✅ builds | I2S standard stereo RX via DMA, 16 kHz, dual IENMP441 on a shared bus |
| `mic_dsp` | ✅ host-tested + builds | NLMS adaptive differential noise canceller (int32 Q15); adapts on noise-only frames |
| `vad` | ✅ host-tested + builds | dual-channel ratio VAD → gap markers (loud ambient rejected) |
| `ring_buffer` | ✅ builds + running | 60 s PSRAM history (retrospective capture) |
| `opus_stream` | ✅ vendored + running | libopus 1.5.2 (fixed-point, no DNN) at `components/opus/opus-1.5.2/`; hand-written IDF build recipe |
| `ble_link` | ✅ builds + running | NimBLE GATT: audio notify / command-write / ack; advertises "OpenSapien" |
| `ble_drain` | ✅ builds + running | core-0 drainer: ring → §C.6 → notify (+ VAD preroll) |
| `commands` | ✅ builds + linked | Ed25519 verify (libsodium) + cJSON parse + dedupe + ack |

Managed components (`espressif/libsodium`, `espressif/cjson`) are declared in
`main/idf_component.yml` and fetched automatically by `idf.py`. A signed §D test
vector for bench-verifying the command path is in `test/command_vector.md`.

### Ed25519 for commands

This IDF's mbedTLS defines the EdDSA constants but ships **no Ed25519 implementation**
(`psa_verify_message` would return `NOT_SUPPORTED`). The `commands` module uses the
**`espressif/libsodium`** managed component (`crypto_sign_ed25519_verify_detached`)
to verify signed §D commands against `SERVER_ED25519_PUBKEY`.

### libopus

libopus 1.5.2 is **vendored** as a local IDF component at
`components/opus/opus-1.5.2/` (hand-written `CMakeLists.txt` and `config.h`,
no upstream meson/autotools). The build is fixed-point, generic-C only — no
x86/arm/mips SIMD (Xtensa LX7 has none), no DNN/Deep-PLC/DRED, no float API.
Host-verified and ESP-IDF-built. See `components/opus/CMakeLists.txt` for the
trim list and feature defines.

## Core layout (dual-core S3)

- **core 1** — audio: I2S stereo DMA → dual-channel VAD → NLMS cancellation → Opus encode → ring buffer
- **core 0** — BLE: drain ring → §C.6 packetise → notify; handle commands

Encode (core 1) and radio (core 0) are split across cores — the reason Spike 1
measured encoder cost in isolation (~6 ms/frame, ~30% of core 1 at complexity 1).

## Dual IENMP441 microphone array

Two IENMP441 (INMP441-class) I2S MEMS microphones on a **shared I2S bus** — both
mics share SCK + WS + SD, and each mic's **L/R channel-select** pin ties it to the
left or right slot. `PRIMARY_CHANNEL` in `config.h` picks which deinterleaved
channel is the **voice/primary** mic (front, faces the wearer) vs the
**noise/reference** mic (back, faces ambient). Flip it by changing the constant —
no rewiring.

### Wiring

| Function | Pin | Silkscreen | Notes |
|---|---|---|---|
| I2S BCK (SCK) | GPIO 4 | D3 | shared bus |
| I2S WS (LRCLK) | GPIO 5 | D4 | shared bus |
| I2S SD (DATA) | GPIO 6 | D5 | shared bus, both mics |
| Mic #1 L/R | GND | — | → left channel (primary/voice) |
| Mic #2 L/R | VDD (3V3) | — | → right channel (reference/ambient) |
| Mic VDD | 3V3 | 3V3 | both mics |
| Mic GND | GND | GND | both mics |

Pins were chosen to avoid the microSD SPI bus (GPIO 7/8/9/21, preserved for future
EOD-video capture), strapping pins (0/3/45/46), and flash/PSRAM (26–32). The old
onboard PDM mic (GPIO 41/42) is removed.

### Signal path

```
I2S std stereo RX (16 kHz, 16-bit, L+R) ── SCK/WS/SD on GPIO 4/5/6 ──
   audio_capture_read_stereo(primary[320], reference[320])   per 20 ms
        │
   vad_process_dual   ── speech iff e_pri > energy_threshold AND e_pri > ratio*e_ref
        │   loud correlated ambient (ratio ≈ 1) → GAP (rejected, no junk)
        │   reference mic silent → falls back to energy-only VAD
        ▼
   mic_dsp_process    ── Widrow NLMS, int32 Q15: out = primary − ĥ(reference)
        │   filter adapts ONLY on noise-only frames (frozen during speech)
        ▼
   opus_stream_encode(out)   ── unchanged mono 24 kbps
        ▼
   ring → §C.6 → BLE   ── UNCHANGED uplink contract
```

### Tuning on hardware

The DSP and VAD tunables in `config.h` are marked `TUNE ON HARDWARE` with safe
defaults (`DSP_NLMS_TAPS 32`, `DSP_NLMS_STEP_Q15 6553`, `VAD_RATIO_THRESHOLD 4`,
`VAD_ENERGY_THRESHOLD 2000000`). To calibrate, add this temporary log line inside
the per-second `if (frames % (1000 / FRAME_MS) == 0)` block in `audio_task`
(`opensapien_sensor.c`), flash, and watch the monitor in three conditions — silence,
ambient noise hitting both mics, and speech toward the front mic:

```c
uint64_t ep = 0, er = 0;
for (int i = 0; i < FRAME_SAMPLES; i++) { ep += (uint64_t)pri[i]*pri[i]; er += (uint64_t)ref[i]*ref[i]; }
ESP_LOGI(TAG, "cal e_pri=%llu e_ref=%llu ratio=%llu",
         (unsigned long long)(ep/FRAME_SAMPLES), (unsigned long long)(er/FRAME_SAMPLES),
         (unsigned long long)((ep/FRAME_SAMPLES)/((er/FRAME_SAMPLES)+1)));
```

Pick `VAD_ENERGY_THRESHOLD` just above the silence floor, `VAD_RATIO_THRESHOLD`
between the ambient ratio (~1) and the speech ratio (typically 4–10×). Raise
`DSP_NLMS_STEP_Q15` toward 9830 (0.3) if cancellation is weak; lower toward 3276
(0.1) if it rings. Remove the temporary log line once tuned.

`esp-nsk` / ESP-SR (Espressif's full AEC+NS+BSS pipeline) was evaluated and
deferred — heavy dependency, uncertain IDF 5.1.6 compatibility, risk to the
working NimBLE/PSRAM setup. The NLMS + ratio-VAD path is the lightweight
alternative; esp-nsk remains a future upgrade path if hardware tuning shows it's
needed.
