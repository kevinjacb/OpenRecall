# OpenRecall

A wearable AI memory device. An ESP32S3 board streams audio over BLE to a phone,
which relays it to a server that transcribes it, extracts memories, and acts on
them.

```
 WEARABLE (firmware)              PHONE (Android relay)            SERVER (Python)
 ───────────────────              ─────────────────────           ───────────────
 §C.6 audio  ──BLE notify──▶  forward verbatim  ──WS binary──▶  ingest + transcribe
 §D command  ◀──BLE write───   forward command   ◀──WS───────   memory + agent
 ack (id)    ──BLE notify──▶  wrap as §E        ──WS cmd_ack─▶  signed §D commands
```

The wearable is **dumb by design**: capture → VAD → Opus → ring buffer → stream,
plus verifying and executing signed commands. No wake word, speech recognition,
embeddings or LLM on the device — all of that is server-side. The phone owns
session framing (§E); the device speaks only §C.6 (audio) and §D (commands).

## Layout

| Path | What |
|---|---|
| `server/` | The brain — WebSocket gateway, HTTP API, transcription, memory, commands. |
| `android/openrecall-relay/` | BLE central → WebSocket bridge. |
| `firmware/openrecall_sensor/` | ESP-IDF firmware for the XIAO ESP32S3 Sense. |
| `deploy/` | Container images, compose, and `deploy.sh`. |

## Run it

**On a laptop**, one process, no hardware:

```bash
cd server
python3 -m venv .venv && source .venv/bin/activate

pip install -e '.[mlx,opus,dev]'              # Apple Silicon  (brew install opus)
pip install -e '.[fasterwhisper,opus,dev]'    # anything else  (apt install libopus0)

python scripts/run_gateway.py
```

It prints the WebSocket URL, the HTTP API URL, the **bearer token** (copy to the
phone) and the **command signing key** (provision on the device). Drive it
without hardware using `python scripts/run_device_sim.py`.

**On an always-on machine**, in containers:

```bash
./deploy/deploy.sh        # detects your hardware, picks a profile, starts it
```

→ **[deploy/README.md](deploy/README.md)** — profiles, GPU setup, backups,
migration, remote access.

## Configure it

**[`server/config.example.toml`](server/config.example.toml) is the single
reference.** Every setting is there with its environment variable and what it
does. Copy it to `server/config.toml`, or export the variables — real
environment variables win over the file.

The four you are most likely to touch:

| Setting | Why |
|---|---|
| `[llm] model` / `base_url` | Memory extraction. Any OpenAI-compatible endpoint. |
| `[embed] model` / `base_url` | Vector search. |
| `[asr] backend` | `whisper` · `parakeet` (both Apple-only) · `faster_whisper` (CPU **and** CUDA). |
| `[inference] url` | Unset runs transcription in-process; set it to run it elsewhere, e.g. on a GPU box. |

A test asserts every variable the code reads is documented there, so it cannot
drift.

## Build the other tiers

```bash
cd android/openrecall-relay && ./gradlew :app:assembleDebug
```

```bash
cd firmware/openrecall_sensor
./scripts/install_idf_5.1.6.sh          # one-shot ESP-IDF v5.1.6 + S3 toolchain
. ~/esp/esp-idf-v5.1.6/export.sh
idf.py build && idf.py -p /dev/cu.usbmodem* flash monitor
```

Provision the server's command public key into `main/config.h`
(`SERVER_ED25519_PUBKEY`) before flashing, or the device rejects every command.
Wiring, pin map and signal path: **[firmware README](firmware/openrecall_sensor/README.md)**.

## Test it

```bash
cd server && .venv/bin/pytest                    # ~1800 tests, no hardware
cd firmware/openrecall_sensor/test && make       # C host tests, no toolchain
cd android/openrecall-relay && ./gradlew test    # JVM, needs JAVA_HOME
```

→ **[docs/TESTING.md](docs/TESTING.md)** — prerequisites, single tests,
troubleshooting.

## More

- **[docs/architecture/ARCHITECTURE.md](docs/architecture/ARCHITECTURE.md)** — how the tiers fit together.
- **[docs/bring-up/](docs/bring-up/)** — validating a real device, tier by tier.

Active personal project. The server is the most mature tier; the firmware builds
clean and the audio path runs on hardware.
