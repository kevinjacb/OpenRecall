# Running the tests — a quick tutorial

OpenRecall has three tiers, and **each one has a test suite you can run with
zero hardware** — just a laptop. This is the crisp version: one section per
tier, prerequisites → command → what success looks like. Pick the tier you're
working in and run that block.

All commands assume you start at the repo root (`/Users/kevin/Projects/Sense`
or your clone). Each tier runs in its own subdirectory.

| Tier | Directory | Stack | Hardware needed |
|---|---|---|---|
| Server | `server/` | Python + pytest | None |
| Android relay | `android/openrecall-relay/` | Kotlin + Gradle | None |
| Firmware | `firmware/openrecall_sensor/` | C host tests / ESP-IDF build | None for host tests; XIAO board for `idf.py flash` |

---

## 1. Server (Python)

The brain: ingest, transcription, memory extraction/retrieval, command
orchestration. ~1500 tests, async (`asyncio_mode = "auto"`), test root is
`server/tests/`.

### One-time setup

```bash
cd server
python3 -m venv .venv
. .venv/bin/activate                 # or use .venv/bin/python directly
pip install -e '.[dev]'              # pytest, ruff, etc.
# Apple-Silicon-only extras (optional, only if you'll run the real engines):
#   pip install -e '.[mlx,opus]'
# Lazy-loaded extras — install ONLY when you want that feature (tests
# skip them if absent):
#   pip install -e '.[denoise]'   # server-side noisereduce denoise
#   pip install -e '.[speaker]'   # real Resemblyzer speaker embedder
#   pip install -e '.[video]'     # video-clip keyframe extraction (Pillow)
#   pip install -e '.[llm]'      # local extraction LLM client
```

`dev` alone is enough to run the full suite — every heavy/optional feature is
lazy-imported, so missing extras make their tests skip rather than fail.

### Run the suite

```bash
cd server
.venv/bin/pytest                     # full suite, quiet
.venv/bin/pytest -q                  # one-line-per-progress quiet form
.venv/bin/python -m pytest           # equivalent if pytest isn't on PATH
```

**Success looks like:** `1529 passed in N.NNs` (the count grows over time; the
exact number isn't load-bearing — what matters is `0 failed`).

### Run a single test / file / keyword

```bash
.venv/bin/pytest tests/test_config_file.py            # one file
.venv/bin/pytest tests/ingest/test_denoise.py::test_noop_passthrough   # one test
. .venv/bin/pytest -k denoise                         # by name substring
.venv/bin/pytest tests/agent/                          # one directory
.venv/bin/pytest --lf                                  # rerun only the last failures
```

### Troubleshooting

- **`ModuleNotFoundError: openrecall_server`** — you skipped `pip install -e
  '.[dev]'` (the `-e` editable install puts `src/` on the path).
- **`cannot import noisereduce`** — that's an optional extra; either
  `pip install -e '.[denoise]'` or ignore (those tests skip, they don't fail).
- **A test needs a model** — some integration tests want `OPENRECALL_LLM_MODEL`
  / `OPENRECALL_EMBED_MODEL` set, or `OPENRECALL_VLM_MODEL=dummy` for CI. If a
  test errors with a connection refused, that's the cause — it's not a code
  regression.

---

## 2. Android relay (Kotlin)

The middle tier: BLE central → WebSocket bridge with session bookkeeping. JVM
unit tests are the **executable protocol spec**. Runs on the JVM — no device,
no emulator.

### Prerequisites

A JDK is required. On macOS the shell has no default JDK, so point it at the
JDK bundled with Android Studio:

```bash
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
```

(Add this to your shell profile if you work in Android often — it's the
single most common reason `./gradlew` fails with "could not find java".)

### Run the suite

```bash
cd android/openrecall-relay
./gradlew test                        # all JVM unit tests
./gradlew :app:testDebugUnitTest      # same tests, explicit task name
```

**Success looks like:** `BUILD SUCCESSFUL`, and the HTML report at
`app/build/reports/tests/testDebugUnitTest/index.html` opens in a browser to
show each passing test.

### Run a single test

```bash
./gradlew :app:testDebugUnitTest --tests "com.openrecall.relay.protocol.*"
./gradlew :app:testDebugUnitTest --tests "*.FrameRoundTripTest"
```

### Build the APK (not a test, but the natural next step)

```bash
./gradlew :app:assembleDebug           # output: app/build/outputs/apk/debug/
```

### Troubleshooting

- **`could not find java` / `JAVA_HOME not set`** — set `JAVA_HOME` as above.
- **`SDK location not found`** — `local.properties` should point at your
  Android SDK install (`sdk.dir=...`); Android Studio creates it on first open.
- **Stale Gradle daemon** — `./gradlew --stop`, then retry.

---

## 3. Firmware (C host tests — no ESP toolchain)

The wearable's **dependency-free pure-C modules** (VAD, C.6 packet, mic DSP,
DC blocker, provisioning, executor core) have host-side contract tests built
with plain `cc`. No ESP-IDF, no toolchain, no board — just a C compiler.

### Run the host tests

```bash
cd firmware/openrecall_sensor/test
make                                  # builds + runs every test target
```

**Success looks like:** each test block prints `[PASS] ...` lines and the run
ends with `ALL PASS`. Individual targets: `make c6`, `make vad`, `make dsp`,
`make dc`, `make provisioning`, `make exec`. Clean artifacts with `make clean`.

### What it checks

These tests byte-match the server's C.6 encoder/decoder contract and validate
each DSP module on the host before the firmware ever touches hardware — so a
failing host test means a real bug, not a flaky device.

### Firmware full build (needs ESP-IDF — separate from the host tests)

The host tests above don't need ESP-IDF. Building the actual firmware does:

```bash
. ~/esp/esp-idf-v5.1.6/export.sh       # in every new shell
cd firmware/openrecall_sensor
idf.py build                           # green = 53% flash free of 3 MB
idf.py -p /dev/cu.usbmodem* flash monitor   # flash + monitor on real hardware
```

If you haven't installed the pinned toolchain yet:

```bash
cd firmware/openrecall_sensor
./scripts/install_idf_5.1.6.sh        # one-shot ESP-IDF v5.1.6 + S3 toolchain
```

---

## Cheatsheet (copy-paste from repo root)

```bash
# Server — full suite
cd server && .venv/bin/pytest -q

# Firmware — host contract tests (no toolchain)
cd firmware/openrecall_sensor/test && make

# Android — JVM unit tests (set JAVA_HOME first)
export JAVA_HOME="/Applications/Android Studio.app/Contents/jbr/Contents/Home"
cd android/openrecall-relay && ./gradlew test
```

Each tier is independent — run one without touching the others. The server is
the most mature tier (green suite, sim-exercised end-to-end); the firmware host
tests are the fastest signal that a DSP/protocol change is correct; the
Android JVM tests are the protocol contract. When in doubt, run the tier you
just changed.