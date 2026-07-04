# Sense AI Sensor — firmware (ESP-IDF)

Product firmware for the wearable (XIAO ESP32S3 Sense). **ESP-IDF**, not Arduino —
this is the efficient foundation: NimBLE (lighter than Bluedroid), DMA I2S capture,
real FreeRTOS core control, mbedTLS for Ed25519, `-O2`. The Phase-0 spike sketches in
`../spike*/` were throwaway; this is the real thing, with their measured parameters
(`main/config.h`) baked in.

## The wearable is dumb (by design)

It does **only**: capture audio → VAD-gate → Opus-encode → PSRAM ring buffer →
stream as **§C.6** over BLE; and receive **§D** signed commands → verify against the
provisioned server key → execute → ack. No wake word, STT, embeddings, or LLM.

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
isn't yet fixed upstream; see `~/.claude/plans/wobbly-tickling-salamander.md`
for the full analysis. Once the bring-up is live end-to-end we can revisit
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
| `audio_capture` | ✅ builds | I2S PDM RX via DMA, 16 kHz mono |
| `vad` | ✅ host-tested + builds | software VAD state machine → gap markers |
| `ring_buffer` | ✅ builds + running | 60 s PSRAM history (retrospective capture) |
| `opus_stream` | ✅ vendored + running | libopus 1.5.2 (fixed-point, no DNN) at `components/opus/opus-1.5.2/`; hand-written IDF build recipe |
| `ble_link` | ✅ builds + running | NimBLE GATT: audio notify / command-write / ack; advertises "Sense" |
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

- **core 1** — audio: PDM DMA → VAD → Opus encode → ring buffer
- **core 0** — BLE: drain ring → §C.6 packetise → notify; handle commands

Encode (core 1) and radio (core 0) are split across cores — the reason Spike 1
measured encoder cost in isolation (~6 ms/frame, ~30% of core 1 at complexity 1).
