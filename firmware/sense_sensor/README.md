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
. $IDF_PATH/export.sh          # ESP-IDF v5.x
idf.py set-target esp32s3
idf.py menuconfig              # confirm PSRAM (Octal), NimBLE, CPU 240 MHz
idf.py build flash monitor
```

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

Builds clean against **ESP-IDF v6.0.1** for `esp32s3` (`idf.py build`): ~197 KB,
81% of the app partition free. `app_main` runs the capture → VAD → ring pipeline on
core 1; flash and watch live `frames/voiced/gap` counts on the serial monitor.

## Module roadmap

| Module | Status | Notes |
|---|---|---|
| `c6_packet` | ✅ host-tested + builds | §C.6 writer, byte-matches server encoder |
| `config` | ✅ builds | Spike-locked params + BLE UUIDs + pins |
| `audio_capture` | ✅ builds | I2S PDM RX via DMA, 16 kHz mono |
| `vad` | ✅ host-tested + builds | software VAD state machine → gap markers |
| `ring_buffer` | ✅ builds | 60 s PSRAM history (retrospective capture) |
| `opus_stream` | 🟡 written, not wired | needs a libopus component (see below) |
| `ble_link` | ✅ builds + linked | NimBLE GATT: audio notify / command-write / ack; advertises "Sense" |
| `commands` | ✅ builds + linked | Ed25519 verify (libsodium) + cJSON parse + dedupe + ack |

Managed components (`espressif/libsodium`, `espressif/cjson`) are declared in
`main/idf_component.yml` and fetched automatically by `idf.py`. A signed §D test
vector for bench-verifying the command path is in `test/command_vector.md`.

### Ed25519 for commands

This IDF's mbedTLS defines the EdDSA constants but ships **no Ed25519 implementation**
(`psa_verify_message` would return `NOT_SUPPORTED`). The `commands` module will use
the **`espressif/libsodium`** managed component (`crypto_sign_ed25519_verify_detached`)
to verify signed §D commands against `SERVER_ED25519_PUBKEY`. Add it on the bench:
`idf.py add-dependency "espressif/libsodium"`.

### Enabling Opus

libopus isn't a first-party IDF component. On the bench (network access), add one —
e.g. vendor `esp-adf`'s or pschatzmann's libopus into `components/`, or a registry
component — then in `main/CMakeLists.txt` add `"opus_stream.c"` to `SRCS` and the
opus component to `REQUIRES`, and uncomment the encode call in `audio_task`. The
module is already written against the standard `opus.h` API with the Spike 1 params.

## Core layout (dual-core S3)

- **core 1** — audio: PDM DMA → VAD → Opus encode → ring buffer
- **core 0** — BLE: drain ring → §C.6 packetise → notify; handle commands

Encode (core 1) and radio (core 0) are split across cores — the reason Spike 1
measured encoder cost in isolation (~6 ms/frame, ~30% of core 1 at complexity 1).
