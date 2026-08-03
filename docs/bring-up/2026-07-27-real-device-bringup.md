# Real-Device Bring-Up Runbook (2026-07-27)

First end-to-end bring-up of the XIAO ESP32S3 Sense → Android relay → Mac
gateway path on real hardware. Everything lives on `main`; all tooling is
already installed. Work bottom-up — **validate each tier before the next** so a
failure points at one layer, not three.

> **Division of labor.** The hands-on steps in this runbook (flashing, serial
> monitor, physical calibration, phone-side connect) are the **operator's**.
> Architecture/code work (the real `SpeakerEmbedder` backend, relay gaps, etc.)
> is separate. Calibration sections are marked **[operator]**.

---

## State going in (be honest about what's verified)

| Tier | Code | On-device verified? |
|---|---|---|
| **Server** (`server/`) | 773 tests green; `run_gateway.py` wired; speaker rec off by default | Mac-only; `run_device_sim.py` has exercised ingest+memory before (`server/data/*.db` exist) |
| **Firmware** (`firmware/sense_sensor/`, ESP-IDF 5.1.6 / NimBLE 1.6) | host tests green, `idf.py build` clean | **No.** The NimBLE 1.7 `xQueueSemaphoreTake uxItemSize == 0` assert on first BLE connect was the reason for the 5.1.6 pin; the on-device result was never confirmed. README "build status" is the claim to verify, not a fact. |
| **Android relay** (`android/sense-relay/`) | protocol brain (`RelaySession`+`Messages`) JVM-unit-tested | **No.** BLE GATT (`SensorLink`), WS transport (`ServerSocket`), foreground service (`RelayService`) all "needs on-device". `request_chunks` backfill NOT handled (audio best-effort). |

---

## Tier 0 — Server baseline (no device, ~2 min)

Establish a known-good server before touching hardware.

```bash
cd /Users/kevin/Projects/Sense/server && source .venv/bin/activate
# heavy extras (one-time): pip install -e '.[mlx,opus]'   # also: brew install opus

# clean slate so old sim data doesn't pollute speaker-recognition tests
rm -f data/*.db data/server_token data/server_ed25519.key

python scripts/run_gateway.py --port 8765 --http-port 8766
```

**Watch for / copy down:**
- `http control API on http://0.0.0.0:8766`
- `gateway listening on ws://0.0.0.0:8765`
- `bearer token (copy to phone): …`
- `server command public key (provision on device): <hex>` ← **save this for Tier 1**
- `speaker recognition DISABLED (set SENSE_SPEAKER_ENABLED=true to enable)`

In another shell, smoke the back-end path the simulator stands in for:
```bash
python scripts/run_device_sim.py
```
Confirm `data/events.db` `capture_events` accumulates rows. If green, the
server is a known-good base; kill the gateway and proceed.

---

## Tier 1 — Firmware audio path (device only, no phone)  [operator]

Goal: capture → VAD → Opus → ring runs on the device, *before* BLE is
exercised. This tier also validates the single-mic VAD calibration.

> **Runtime path is single-mic energy VAD** (`vad_process_single`): speech iff
> primary mean-square > `VAD_ENERGY_THRESHOLD`. The dual-channel ratio gate
> (`vad_process_dual`) and NLMS canceller (`mic_dsp_process`) are **bypassed at
> runtime** — INMP441 is omnidirectional; front/back separation is only ~1.5–2×
> (ratio ~1), so the ratio gate rejected voice and NLMS cancelled it. They stay
> in code + host tests for future use if real acoustic shadowing ever gives
> ratio >> 1. The firmware README "Signal path" diagram shows the dual path as
> design intent, not runtime truth.

```bash
. ~/esp/esp-idf-v5.1.6/export.sh    # new shell each time

# Provision: paste the Tier 0 server pubkey hex into main/config.h
#   SERVER_ED25519_PUBKEY  (the device verifies command sigs against it)
cd /Users/kevin/Projects/Sense/firmware/sense_sensor
idf.py build
idf.py -p /dev/cu.usbmodem* flash monitor
```

**Watch the monitor for:**
- clean boot (no panic/backtrace)
- per-second log line: `frames=… voiced=… gap=…` at ~50 fps
- `opus_bytes/s > 0` once a subscriber is attached (won't show until Tier 2,
  but the encode loop should run regardless)

### [operator] Single-mic VAD calibration

The `VAD_ENERGY_THRESHOLD` / `VAD_RATIO_THRESHOLD` / `DSP_NLMS_*` tunables in
`main/config.h` are marked `TUNE ON HARDWARE` with safe defaults
(`VAD_ENERGY_THRESHOLD 2000000`). Calibrate by adding this temporary log line
inside the per-second `if (frames % (1000 / FRAME_MS) == 0)` block in
`audio_task` (`main/sense_sensor.c`), flash, and watch in three conditions —
**silence, ambient noise hitting both mics, speech toward the front mic at
wearable distance (~10–15 cm)**:

```c
uint64_t ep = 0, er = 0;
for (int i = 0; i < FRAME_SAMPLES; i++) { ep += (uint64_t)pri[i]*pri[i]; er += (uint64_t)ref[i]*ref[i]; }
ESP_LOGI(TAG, "cal e_pri=%llu e_ref=%llu ratio=%llu",
         (unsigned long long)(ep/FRAME_SAMPLES), (unsigned long long)(er/FRAME_SAMPLES),
         (unsigned long long)((ep/FRAME_SAMPLES)/((er/FRAME_SAMPLES)+1)));
```

Expected: `e_pri` (left/mouth) exceeds ~2e5 during speech with `voiced>0`.
Sensitivity finding on record: at arm's length the INMP441 collapses to
amp ~150–230 (ms ~2e4–5e4), indistinguishable from room ambient — **the device
must be worn at wearable distance**; no threshold discriminates quiet distant
speech from ambient.

Pick `VAD_ENERGY_THRESHOLD` just above the silence floor. (The ratio gate is
off at runtime, so `VAD_RATIO_THRESHOLD` and `DSP_NLMS_STEP_Q15` only matter
if you re-enable the dual path later — raise NLMS step toward 9830/0.3 if
cancellation is weak, lower toward 3276/0.1 if it rings.) **Remove the temp
log line once tuned.**

**Exit criteria:** device boots clean, audio task runs at ~50 fps, calibration
log confirms speech discriminates from ambient at wearable distance. Then
Tier 2.

---

## Tier 2 — Full XIAO → Android relay → Mac path  [operator]

This is the **NimBLE 1.6 assert smoke** — the first BLE GAP connect is the
landmine the whole IDF 5.1.6 pin exists to dodge.

Start the gateway on the Mac (Tier 0 command; note your Mac's LAN IP, e.g.
`192.168.1.20`). Then:

```bash
cd /Users/kevin/Projects/Sense/android/sense-relay
./gradlew test                 # RelaySessionTest — the executable spec
./gradlew :app:assembleDebug   # builds the APK
```

Install the debug APK on the phone. Start the relay pointing at the gateway:
```kotlin
startForegroundService(Intent(ctx, RelayService::class.java)
    .putExtra("server_url", "ws://192.168.1.20:8765"))
```
Grant `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT` at runtime (Android 12+).

**Watch — three places at once:**
1. **Device monitor**: `ble_link` advertises "Sense", accepts the GAP connect.
   The critical moment: **no `xQueueSemaphoreTake uxItemSize == 0` assert.** If
   it asserts here, the 5.1.6 pin didn't fix it — stop and capture the
   backtrace.
2. **Phone (logcat)**: `SensorLink` scans → connects → subscribes to the audio
   notify characteristic; `ServerSocket` opens the WS to the gateway.
3. **Gateway log**: `connection opened from <phone>`, hello, then
   `capture_events` rows accumulating and `transcript` frames appearing as you
   speak.

**Known landmines on this tier:**
- Android GATT is handset-dependent — operations must be serialised (the relay
  uses `opQueue`), and MTU negotiation / notification delivery vary. If audio
  stalls, check subscribe order + MTU first.
- `request_chunks` backfill is **not handled** by the relay yet (logged, not
  forwarded). Audio is best-effort; durability comes from the device ring
  buffer + resync on reconnect. Gaps during packet loss are expected for now.

**Exit criteria:** speak at the device → transcripts appear in the gateway
log → `capture_events` accumulates. Then Tier 3.

---

## Tier 3 — Speaker recognition on real audio  [operator]

Validates the speaker-recognition **wiring** (shipped, off by default) on real
audio. Uses the **fake embedder** first — it proves the plumbing end-to-end
without needing a real model.

```bash
SENSE_SPEAKER_ENABLED=true python scripts/run_gateway.py --port 8765
# expect: "speaker recognition ENABLED (model=fake)"
```

Reconnect the relay and talk. **Watch the gateway log for:**
- **One voice (the wearer)**, sustained: the cold-start "You" enrollment tags
  the dominant voice; after `confirm_turns` (default 10) the **confirm nudge**
  fires — `proactive` frame: *"I've been hearing one main voice — is that you?"*
- **A second, distinct voice**, recurring: corroboration-before-mint accumulates
  embeddings; once corroborated, the **name nudge** fires carrying
  `propose={"kind":"name_speaker","speaker_id":"…"}` plus 2–3 sample transcript
  lines.
- **Mis-attribution**: send a `ReassignSpeaker` control message
  (`{from_speaker_id, to_speaker_id, scope: one|range|all}`) — events + atoms
  relabel across sessions and the ring-buffer embeddings move between
  centroids (de-poisoning). Confirm the relabel in `capture_events` / `atoms.db`.

> The fake embedder's "distance" is synthetic, so this tier proves the
> **plumbing** (config → identifier → transcript → events/atoms → nudge →
> correction), **not** real speaker separation. Real separation is the next
> architecture build — a local `SpeakerEmbedder` backend (SpeechBrain
> ECAPA-TDNN / Resemblyzer, CPU-runnable, lazy-imported behind the
> `SpeakerEmbedder` Protocol) so `SENSE_SPEAKER_ENABLED=true` does actual
> recognition. Validate against two real voices.

**Exit criteria:** confirm nudge + name nudge fire on the right voices;
`ReassignSpeaker` relabels cleanly. Bring-up complete.

---

## Tier 4 — Command executors (P4a: request_buffer + start/stop_audio)  [operator]

Validates the device-side executor framework + the three audio executors.
Requires Tiers 0–2 (a live gateway + relay path). The executors are exercised by
issuing signed commands from the server (the planner's `issue_command` path) or
the HTTP control API; the device acks and acts.

Start the gateway (Tier 0 command). Reconnect the relay. Then issue commands and
**watch the device monitor + gateway log**:

- **`stop_audio`** (e.g. ask the agent "stop listening for a bit", or POST an
  `issue_command` with `type=stop_audio`):
  - Device monitor: `exec: stop_audio`, then the audio per-second log shows
    `voiced=0 (no new speech) opus_bytes/s=0` and `cal e_pri=0 e_ref=0 ratio=0`.
  - Gateway: only empty live packets (`C6_GAP_MARKER`) arrive; no transcripts
    while paused. The ring keeps advancing (contiguous).
- **`start_audio`**:
  - Device monitor: `exec: start_audio`; speech at the device resumes
    transcription. `voiced>0` returns.
- **`request_buffer seconds=5`** (e.g. "play back the last 5 seconds"):
  - Device monitor: `exec` enqueues, then `drain: replay done: seconds=5 frames=250
    chunk_seq->N`. `C6_MEMORY_CHUNK` packets are notified, the last one with
    `C6_FLAG_LAST_OF_REQ`.
  - Gateway: a retrospective transcript segment appears for the last ~5 s
    (the reassembler orders the MEMORY_CHUNK packets by chunk_seq after the
    live head).
- **`request_buffer seconds=60` on a freshly-booted device** (<60 s of audio
  captured): device logs `replay capped: requested 60 s, have <3000> frames` and
  still emits a final `LAST_OF_REQ` packet.
- **`capture_photo` / `record_video`** (only if you manually issue one — they are
  in the server allowlist but not implemented in P4b yet):
  - Device monitor: `exec: capture_photo not implemented (P4b)`. The command acks
    (valid, authentic) but does nothing. Expected in P4a; do not file a bug.

**Exit criteria:** stop/start gate visibly silences/resumes transcription;
`request_buffer` produces a retrospective transcript segment with a clean
`LAST_OF_REQ` boundary; the capped-replay edge case logs correctly. Then the
P4a executors are verified on real hardware.

---

## Do not undo (already shipped)

- Firmware VAD threshold 2e5→5e4 (commit `02580b3`) — don't raise it back to
  "fix" distance issues; the sensitivity finding is the real cause (wearable
  distance required).
- Server whisper hallucination filter (commit `2ea3e3e`) — don't re-enable the
  NLMS canceller on the runtime path (two omni mics → ratio ~1 → it cancels the
  voice).
- Speaker-recognition v1 (off by default; 773 tests green) — additive-only.

## Quick command reference

```bash
# Server
cd /Users/kevin/Projects/Sense/server && source .venv/bin/activate
python scripts/run_gateway.py --port 8765 --http-port 8766
SENSE_SPEAKER_ENABLED=true python scripts/run_gateway.py --port 8765   # Tier 3

# Firmware
. ~/esp/esp-idf-v5.1.6/export.sh
cd /Users/kevin/Projects/Sense/firmware/sense_sensor
idf.py build && idf.py -p /dev/cu.usbmodem* flash monitor

# Android relay
cd /Users/kevin/Projects/Sense/android/sense-relay
./gradlew test && ./gradlew :app:assembleDebug

# Server tests (regression guard)
cd /Users/kevin/Projects/Sense/server && source .venv/bin/activate && python -m pytest -q
```