# OpenRecall

A wearable AI memory device. A small ESP32S3 Sense board on the wearer streams
audio over BLE to a phone, which relays it to a server that transcribes, extracts
memories, and acts on them. Three tiers, each independently buildable and testable:

```
 WEARABLE (firmware)              PHONE (Android relay)            SERVER (Python)
 ───────────────────              ─────────────────────           ───────────────
 §C.6 audio  ──BLE notify──▶  forward verbatim  ──WS binary──▶  ingest + transcribe
 §D command  ◀──BLE write───   forward command   ◀──WS───────   memory + agent
 ack (id)    ──BLE notify──▶  wrap as §E        ──WS cmd_ack─▶  signed §D commands
                               (owns hello/bye,
                                acks, transcripts)
```

The wearable is **dumb by design**: it does capture → VAD → Opus → ring buffer →
stream, and verifies+executes signed commands. No wake word, STT, embeddings, or
LLM on the device — all of that lives on the server. The phone relay owns session
framing (§E); the device speaks only §C.6 (audio) + §D (commands).

## Repository layout

| Path | What |
|---|---|
| `server/` | The brain — Python (aiohttp) WebSocket gateway + HTTP control API: ingest, transcription, memory extraction/retrieval, device command orchestration. |
| `android/openrecall-relay/` | The middle tier — Kotlin app: BLE central → WebSocket bridge with session bookkeeping (§E). |
| `firmware/openrecall_sensor/` | The wearable — ESP-IDF firmware for the XIAO ESP32S3 Sense (NimBLE, DMA I2S, Opus, Ed25519 command verify). |
| `firmware/spike1_opus_encode/`, `firmware/spike2_sd_throughput/` | Throwaway measurement sketches that baked the real firmware's parameters. |
| `docs/bring-up/` | Real-device bring-up runbook (tier-by-tier validation). |
| `.env.example` | Server config reference (copy to `.env`). |

## Quick start

### 1. Server (Mac/Linux, no hardware needed)

```bash
cd server
python -m venv .venv && source .venv/bin/activate
pip install -e '.[mlx,opus,dev]'     # mlx + opus are Apple-Silicon-only; brew install opus
# Optional heavy extras: pip install -e '.[speaker,llm]'

# Clean slate (optional, removes old sim data):
rm -f data/*.db data/server_token data/server_ed25519.key

python scripts/run_gateway.py --port 8765 --http-port 8766
```

On startup the gateway prints:
- `gateway listening on ws://0.0.0.0:8765` — the WS endpoint the relay connects to.
- `http control API on http://0.0.0.0:8766` — operator/phone-facing control API.
- `bearer token (copy to phone): …` — the relay authenticates with this.
- `server command public key: <hex>` — **provision this on the device** (Tier 2).

Speaker recognition is **off by default**; set `OPENRECALL_SPEAKER_ENABLED=true` to enable.
Without a device you can still exercise the back-end with the simulator:

```bash
python scripts/run_device_sim.py     # stands in for device + phone combined
```

Confirm `server/data/events.db` accumulates `capture_events` rows.

### 2. Android relay (phone)

```bash
cd android/openrecall-relay
./gradlew test                    # JVM unit tests — the executable protocol spec
./gradlew :app:assembleDebug      # build the APK (or open in Android Studio)
```

Point the relay at your gateway (host IP; `10.0.2.2` is host-loopback from the
emulator) and grant `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT` at runtime (Android 12+):

```kotlin
startForegroundService(Intent(ctx, RelayService::class.java)
    .putExtra("server_url", "ws://192.168.1.20:8765"))
```

See `android/openrecall-relay/README.md` for the architecture and current on-device gaps.

### 3. Firmware (XIAO ESP32S3 Sense)

```bash
cd firmware/openrecall_sensor
./scripts/install_idf_5.1.6.sh     # one-shot: ESP-IDF v5.1.6 + S3 toolchain (side-by-side)
. ~/esp/esp-idf-v5.1.6/export.sh  # in every new shell

# Provision: paste the server's command public key hex into main/config.h
#   SERVER_ED25519_PUBKEY   (the device verifies §D command signatures against it)

idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor
```

We pin to **ESP-IDF v5.1.6** (NimBLE 1.6). See `firmware/openrecall_sensor/README.md` for
the full module roadmap, core layout, signal path, and on-hardware tuning guide.

### Wiring essentials

Two IENMP441 (INMP441-class) I2S MEMS microphones on a **shared I2S bus**; each mic's
L/R channel-select pin ties it to the left or right slot. `PRIMARY_CHANNEL` in
`config.h` picks which deinterleaved channel is the voice/primary mic — no rewiring
to swap. Full details and the signal-path diagram are in the firmware README.

| Function | Pin | Notes |
|---|---|---|
| I2S BCK (SCK) | GPIO 4 | shared bus |
| I2S WS (LRCLK) | GPIO 5 | shared bus |
| I2S SD (DATA) | GPIO 6 | shared bus, both mics |
| Mic #1 L/R select | GND | → left channel (primary/voice) |
| Mic #2 L/R select | VDD (3V3) | → right channel (reference/ambient) |
| Mic VDD / GND | 3V3 / GND | both mics |

> **Schematics:** full schematics will be added here. (Placeholder — to be filled in.)

Pins avoid the microSD SPI bus (GPIO 7/8/9/21, reserved for future EOD-video
capture), strapping pins (0/3/45/46), and flash/PSRAM (26–32).

## Configuration

Server config is environment-driven. Copy `.env.example` → `.env` and adjust. Every
variable is optional; the server falls back to safe defaults. Models are
**provider-agnostic** (OpenAI-compatible) — local (Ollama at `:11434` by default,
`mlx_lm.server`, vLLM, LM Studio) or cloud; pick per family:

- `OPENRECALL_LLM_MODEL` / `OPENRECALL_LLM_BASE_URL` — the agent's reasoning model.
- `OPENRECALL_EMBED_MODEL` / `OPENRECALL_EMBED_BASE_URL` — vector-search embedder.
- `OPENRECALL_VLM_MODEL` / `OPENRECALL_VLM_BASE_URL` — image atoms (only if you ingest images).

The bearer token and Ed25519 signing key are auto-generated on first run into
`server/data/`; override paths with `OPENRECALL_TOKEN_FILE` / `OPENRECALL_KEY_FILE` if migrating.

## Testing

```bash
# Server (no hardware):  pytest with pythonpath=src, asyncio_mode=auto
cd server && pytest

# Firmware host contract tests (no hardware): byte-matches the server's §C.6 encoder
cd firmware/openrecall_sensor/test && make

# Android relay protocol brain (JVM):
cd android/openrecall-relay && ./gradlew test
```

## Status

This is an active personal project; the server is the most mature tier (green test
suite, sim-exercised end-to-end). The firmware builds clean and the audio path runs,
but on-device BLE bring-up is the active step — see `docs/bring-up/` for the tiered
runbook (validate each tier before the next so a failure points at one layer).