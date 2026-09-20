# OpenRecall — System Architecture

> How all of it comes together: firmware, Android relay, and server — every
> protocol, every algorithm, every wire format, every state machine.
>
> This is the exhaustive reference. It is grounded in the source: every claim
> carries a `file:line` citation so a reader can jump straight to the code.
> Paths are relative to the repo root (`/Users/kevin/Projects/Sense/`).

> [!IMPORTANT]
> **Scope and currency.** Written 2026-08-08 and accurate for the three tiers it
> describes: the capture path, the wire protocols (§C.6, §D, §E), the ingest and
> memory pipelines, and the command channel. Those are stable and this remains
> the reference for them.
>
> It **predates** the deployment work and does not cover:
>
> - the **inference boundary** — ASR and speaker embedding can run in a separate
>   process (`server/src/openrecall_server/inference/`), selected by
>   `[inference] url`;
> - the **portable ASR backend** — `faster_whisper` runs on CPU and CUDA, where
>   this document assumes MLX and therefore Apple Silicon;
> - **containers and deployment profiles**, and remote access via Cloudflare
>   Tunnel.
>
> For those, see **[deploy/README.md](../../deploy/README.md)**. Where this
> document and the code disagree, the code is right — the `file:line` citations
> are the way to check.

---

## 0. The thirty-second version

OpenRecall is a **wearable AI memory platform** with three tiers:

```
   OpenRecall Sensor              OpenRecall Relay              OpenRecall Server
   (firmware, ESP-IDF)            (Android, Kotlin)             (Python, aiohttp)
   ───────────────────            ──────────────────            ──────────────────
   XIAO ESP32S3 Sense            Android phone                 Mac / box
   capture → VAD → Opus           BLE ↔ WS bridge               decode → transcribe
   → 60s PSRAM ring              (relay-owned §E)               → speaker ID
   → §C.6 over BLE                → signed §D passthrough        → memory extraction
   ← §D signed commands          ← §E transcripts/proactive     → agent + proactive
   verify → execute → ack        → HTTP speaker rename          → §D command issue
```

The wearable is deliberately dumb: it captures audio, gates it with VAD, Opus-encodes
it, buffers it in a 60s PSRAM ring, and streams it as **§C.6** binary frames over
BLE. It receives **§D** Ed25519-signed commands, verifies them against a
provisioned server key, executes them, and acks. It does **not** do §E session
framing — that is the phone relay's job (`firmware/openrecall_sensor/main/openrecall_sensor.c:4-7`).

The Android relay is a thin bridge: it forwards §C.6 audio **verbatim** (raw BLE
notification bytes → WebSocket binary frame), re-wraps signed §D commands from the
server's §E envelope into the device's on-wire frame, wraps device acks back into §E,
and owns the text-JSON control plane. It cannot forge commands — the device verifies
every signature.

The server is the brain: it decodes §C.6, reassembles the ordered stream,
transcribes it (streaming mlx-whisper), identifies speakers (Resemblyzer +
corroboration-before-mint), forms memories (60s-windowed LLM extraction), indexes
them, answers questions (a retrieval + LLM planner with confidence-gated
guardrails), proactively surfaces insights, and issues signed device commands.

Three protocols bind the tiers:

| Protocol | Carries | Direction | Transport |
|----------|---------|-----------|-----------|
| **§C.6** | Opus audio frames (binary) | device → server | BLE notify → WS binary (relay verbatim) |
| **§D** | Signed device commands | server → device | WS text (§E-wrapped) → BLE write (relay re-wraps) |
| **§E** | Session control (text JSON) | bidirectional | WS text, relay-owned |

---

## 1. The three protocols in detail

### 1.1 §C.6 — audio wire format (binary)

The firmware emits binary packets over a BLE notify characteristic; the relay
forwards them **verbatim** as WebSocket binary frames; the server decodes them.
There is no shared library — the layout is dual-implemented (C on the device,
Python on the server) and kept in lockstep by a host-compilable C contract test
that byte-matches a server-generated golden vector.

**Canonical definitions:**

Firmware — `firmware/openrecall_sensor/main/config.h:75-80`:
```c
#define C6_VERSION 1
#define C6_HEADER_LEN 12
#define C6_FLAG_HISTORICAL 0x01
#define C6_FLAG_LAST_OF_REQ 0x02
#define C6_FRAMES_PER_CHUNK 50   /* 50 frames == 1 s of audio per burst */
enum c6_packet_type { C6_LIVE = 0, C6_MEMORY_CHUNK = 1, C6_HISTORICAL = 2 };
enum c6_vad_state    { C6_GAP_MARKER = 0, C6_SPEECH = 1, C6_PREROLL = 2, C6_HANGOVER = 3 };
```

Server — `server/src/openrecall_server/ingest/audio_packet.py:28-38`:
```python
class PacketType(IntEnum):  LIVE = 0; MEMORY_CHUNK = 1; HISTORICAL = 2
class VadState(IntEnum):    GAP_MARKER = 0; SPEECH = 1; PREROLL = 2; HANGOVER = 3
```

All numeric values match across both ends. Flag bits are inline masks, not a
named enum (`audio_packet.py:61-63`): `is_last_of_request → bool(self.flags & 0b10)`.

**Byte-level header** (12 bytes, little-endian), documented identically in
`firmware/openrecall_sensor/main/c6_packet.h:8-15` and
`server/src/openrecall_server/ingest/audio_packet.py:2-16`:

```
byte 0       (version << 4) | ptype     version nibble hi, ptype nibble lo
bytes 1-4    chunk_seq        (uint32 LE) boot-relative monotonic, never resets
bytes 5-8    rel_ts_ms        (uint32 LE) per-device monotonic ms (not wall clock)
byte 9       vad_state
byte 10      frame_count
byte 11      flags            bit0=historical, bit1=last-of-request
byte 12..    repeated [u8 len][opus bytes] × frame_count
```

Server decoder: `_HEADER = struct.Struct("<BIIBBB")` (`audio_packet.py:24`).
Firmware encoder: writes byte 0 as `((C6_VERSION & 0x0F) << 4) | (ptype & 0x0F)`,
then `memcpy`s the two uint32s (`firmware/openrecall_sensor/main/c6_packet.c:24-30`).

**Opus payload:** each frame is `[u8 len][len bytes of Opus]`. Frame length capped
at 255 (u8 field); `MAX_OPUS_BYTES = 256` (`config.h:24`). Audio is Opus 16 kHz
mono, 20 ms frames (`FRAME_MS=20`), 24 kbps (`OPUS_BITRATE=24000`), complexity 1
(`config.h:22-23`). 50 frames per chunk = 1 second (`config.h:80`).

**chunk_seq numbering:** a boot-relative monotonic uint32 that never resets
(`firmware/openrecall_sensor/main/ble_drain.c:166` `uint32_t chunk_seq = 0;`,
incremented per emitted packet). The server's `SessionReassembler` orders packets
by this seq:
- `start_seq=0` (live) → reassembler latches at the **first packet it sees** and
  drops head packets (`reassembler.py:118-124`). Real bring-up shows the device's
  first observed chunk_seq ~26170 because the device was running before the phone
  subscribed.
- `start_seq>0` (resume) → waits for that anchor, tolerates reordering
  (`reassembler.py:39-46`).
- `seq < next_expected` → idempotent drop; `seq > next_expected` → buffered as
  future packet + head-gap reported; `seq == next_expected` → deliver frames +
  drain contiguous tail (`reassembler.py:130-152`).

**GAP_MARKER and LAST_OF_REQ semantics:**
- `C6_GAP_MARKER` (vad_state=0): VAD-suppressed silence. Carries `frame_count=0`;
  advances chunk_seq without emitting audio; NOT a dropout (`reassembler.py:144-151`).
  The live drain path emits one empty GAP_MARKER per silent batch so chunk_seq keeps
  advancing (`ble_drain.c:273-283`).
- `C6_FLAG_LAST_OF_REQ` (0x02): marks the final packet of a `request_buffer`
  retrospective replay (`ble_drain.c:111,132,151`). The server parses and exposes it
  (`audio_packet.py:61-63`) but treats the packet as a normal in-order delivery;
  callers use the flag to know the replay ended. Replay packets are `C6_MEMORY_CHUNK`
  (ptype=1), not `C6_LIVE`.

**BLE MTU / chunk sizing:** the phone requests MTU 247 on connect
(`android/.../ble/SensorLink.kt:121`). Firmware sizes each BLE notification to
`(mtu - 3)` payload bytes (3-byte ATT header) — `ble_drain.c:269-271`. It packs as
many Opus frames as fit into `budget = max_payload - C6_HEADER_LEN`, each packet
with its own chunk_seq. A BLE notification is a single PDU that does NOT fragment,
so an oversized packet would be silently truncated on-air; the firmware guards
against this (`ble_drain.c:296-306` drops a frame larger than budget).

### 1.2 §D — signed commands (Ed25519)

**Server signing** (`server/src/openrecall_server/commands/signing.py:56-78`):
`CommandSigner.sign(command)` signs `command.canonical_bytes()` with a raw
64-byte detached Ed25519 signature (`cryptography...Ed25519PrivateKey.sign()`).
The private key is loaded/created by `load_or_create_signer(path)` (`signing.py:91-108`)
— raw 32 bytes at `data/server_ed25519.key`, 0600. Stable across restarts.

**Canonical serialization** (`commands/model.py:49-55`):
```python
def canonical_bytes(self) -> bytes:
    return json.dumps(self.model_dump(mode="json"), sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
```
Deterministic: `sort_keys=True` + compact separators. Insertion-order independent
(`server/tests/commands/test_model.py:31-38`).

**Command fields** (`commands/model.py:34-47`): `command_id`, `session_id`, `type`
(the allowlist `CommandType` Literal), `params`, `issued_at`, `expires_at`,
`idempotency_key`. All fields are signed. No separate nonce — `command_id`
(uniqueness) + `issued_at`/`expires_at` (freshness) serve that role. `expires_at`
is NOT checked on-device; the relay enforces expiry upstream
(`firmware/openrecall_sensor/main/commands.h:9-11`).

**Two-layer wire structure:**
- Layer A — signed envelope (`signing.py:39-53` `to_wire()`):
  `{"payload": <canonical JSON string>, "sig": <base64 of 64-byte signature>}`.
  `from_wire` re-parses `payload` — never trusts a separately-supplied body.
- Layer B — §E WS control message (`protocol/messages.py:95-103`):
  `{"type":"command","session_id":...,"payload":...,"sig":...}`.

**Command type allowlist:**
- Wire-model allowlist (`commands/model.py:22-31`), a pydantic `Literal`:
  `capture_photo, record_video, start_audio, stop_audio, play_audio, display_text,
  show_status, request_buffer` (8 types).
- Autonomous-execution allowlist (`agent/validator_command.py:44-50`), the binding
  contract for "firmware knows how to execute this":
  `{capture_photo, record_video, start_audio, stop_audio, request_buffer}` (5).
  `display_text`/`play_audio`/`show_status` are in the wire Literal but NOT in the
  allowlist — they fail server validation as `UNKNOWN_TYPE`. Per-type param schemas
  (`validator_command.py:78-108`): `record_video` = `duration_s` ∈ [1,30];
  `request_buffer` = `seconds` ∈ [1,60]; others no params.

**Firmware verify path (libsodium, NOT mbedTLS):** mbedTLS on this IDF has no
Ed25519, so the device uses libsodium (`firmware/openrecall_sensor/main/commands.c`):
```c
#include "sodium.h"
#define ED25519_SIG_LEN 64
static uint8_t s_pubkey[32];
int commands_init(const uint8_t server_pubkey[32], command_ack_fn ack) {
  if (sodium_init() < 0) { return -1; }
  memcpy(s_pubkey, server_pubkey, sizeof s_pubkey);
  s_ack = ack; ...
}
```
On-wire frame on BLE (`commands.h:4-5`): `[64-byte raw Ed25519 signature] || [canonical payload JSON]`.

Verify (`commands.c:67-79`):
```c
void commands_handle(const uint8_t *data, size_t len) {
  if (len <= ED25519_SIG_LEN) { return; }
  const uint8_t *sig = data;
  const uint8_t *payload = data + ED25519_SIG_LEN;
  size_t payload_len = len - ED25519_SIG_LEN;
  if (crypto_sign_ed25519_verify_detached(sig, payload, payload_len, s_pubkey) != 0) {
    ESP_LOGW(TAG, "command signature INVALID — dropped");  // no ack
    return;
  }
  ...parse + dedupe + execute...
}
```
On failure: dropped, **no ack**. The relay cannot forge or tamper without breaking
the signature — the trust boundary is end-to-end (server→device).

**Ack format (both ends match):** firmware `ack(command_id)` calls
`s_ack((uint8_t*)command_id, strlen)` → `ble_link_notify_ack` (`ble_link.c:213-215`)
pushes the **raw command_id string** as a BLE notify on the ACK characteristic.
Server `CommandAck` (`protocol/messages.py:41-46`): `{"type":"command_ack",
"session_id":...,"command_id":...}`. Both ends key on `command_id`; the relay adds
`session_id` + `type`.

**Test vectors** (`firmware/openrecall_sensor/test/command_vector.md`): two 213-byte
golden frames — VALID (verify, execute `capture_photo`, ack `demo:0`) and FORGED
(first sig byte flipped one bit; device drops, no ack). Provisioning key:
`03a2a60b...`. These are manual bench vectors; `test_executors.c` tests
parse/validate/dispatch logic (not the libsodium verify).

### 1.3 §E — session framing (text-JSON control plane, relay-owned)

There is no single enum — each side uses discriminated unions on a `type` string.
Server models in `server/src/openrecall_server/protocol/messages.py` all extend
`_Strict` → `ConfigDict(extra="forbid")` (`messages.py:19-20`).

| Kind literal | Direction | Server def | Relay def |
|--------------|-----------|------------|-----------|
| `hello` | relay → server | `messages.py:29` | `Messages.kt:26` |
| `bye` | relay → server | `messages.py:37` | `Messages.kt:34` |
| `command_ack` | relay → server | `messages.py:44` | `Messages.kt:39` |
| `ack` | server → relay | `messages.py:67` | `Messages.kt:56` (Ack) |
| `request_chunks` | server → relay | `messages.py:75` | `Messages.kt:56` (RequestChunks) |
| `transcript` | server → relay | `messages.py:84` | `Messages.kt:56` (Transcript) |
| `command` | server → relay | `messages.py:95` | `Messages.kt:56` (Command) |
| `proactive` | server → relay | `messages.py:106` | `Messages.kt:56` (Proactive) |

**Inbound (relay → server)** — `messages.py:26-53`:
- `Hello`: `session_id`, `start_seq: int = 0`
- `Bye`: `session_id`
- `CommandAck`: `session_id`, `command_id`

**Outbound (server → relay)** — `messages.py:64-122`:
- `Ack`: `session_id`, `next_seq`
- `RequestChunks`: `session_id`, `start`, `end`
- `TranscriptMsg`: `session_id`, `text`, `duration_ms`, `speaker: str|None`,
  `speaker_confidence`, `speaker_assignment`, `speaker_name`, `is_wearer`
- `CommandMessage`: `session_id`, `payload`, `sig`
- `ProactiveMessage`: `session_id`, `request_id`, `text`, `atoms`, `propose: dict|None`

**Relay side** (`android/.../protocol/Messages.kt`): outbound `Hello`/`Bye`/`CommandAck`
are `@Serializable`, encoded with `Json{ignoreUnknownKeys=true; encodeDefaults=true}`
(`Messages.kt:20-22`). Inbound `ServerMessage` is a sealed hierarchy
(`Messages.kt:56-89`): `Ack(nextSeq)`, `RequestChunks(start,end)`,
`Transcript(text,durationMs,speaker?,speakerName?,isWearer)`, `Command(payload,sig)`,
`Proactive(requestId,text,atoms,propose?)`, `Unknown(type)`. The relay `Transcript`
carries `speaker`/`speakerName`/`is_wearer` but NOT `confidence`/`assignment` (those
don't exist on the relay side). Lenient parsing never throws (`Messages.kt:92-123`);
malformed → `Unknown("malformed")`, unknown type → `Unknown(type)`.

**The JsonNull → Kotlin null fix** (`Messages.kt:95-99`): the server emits a present
JSON `null` (`"speaker":null`) for a hop with no resolved speaker. Without the guard
`e.jsonPrimitive.content` on `JsonNull` returns the literal string `"null"` (because
`JsonNull` IS a `JsonPrimitive` whose `.content == "null"`). The `if (e is JsonNull)
return null` line surfaces Kotlin `null` instead. This was RC1 of the rename/null fix
(`server/tests/...`/`MessagesParseTest.kt:40-52`).

**`name_speaker` / `reassign` — retired to HTTP:** `name_speaker` appears ONLY nested
inside `ProactiveMessage.propose` as `{"kind":"name_speaker","speaker_id":...}`,
emitted by `SpeakerNudgeListener` (`agent/speaker_nudge.py:100`). The relay parses it
via `parsePropose` (`Messages.kt:126-132`), recognizing only `kind=="name_speaker"`.
`reassign`/`rename` actions are **HTTP-only**: `POST /speakers/{id}/rename` and
`POST /speakers/reassign` (`http/routes/speakers.py:15-18`). There is no §E `reassign`
inbound branch — it would fall to `Unknown` and be ignored.

**Reconnect semantics — continue from last_event, not from 0:** see §6.3 for the full
treatment. The relay always sends `start_seq=0` (`RelaySession.kt:27-28,46`;
`RelayService.kt:139-142`); the server-side `GatewayCore._on_hello` continues the
event counter from `store.last_event()` so post-reconnect transcripts get fresh
event_ids instead of colliding/dropping (`gateway/core.py:248-276`).

---

## 2. Tier 1 — Firmware (`firmware/openrecall_sensor`)

ESP-IDF v5.1.6 / NimBLE 1.6 (pinned — Seeed XIAO ESP32S3 Sense, dual-core Xtensa LX7,
8 MB Octal PSRAM, dual IENMP441 I2S mic array). The wearable is "deliberately dumb"
(`openrecall_sensor.c:4-7`).

### 2.1 Module inventory (18 files in `main/`)

| File | Role |
|------|------|
| `openrecall_sensor.c` | App entry: `app_main`, `audio_task`, core/priority layout |
| `config.h` | Every `#define`: audio, VAD, DSP, C6, BLE UUIDs, GPIO, executor |
| `audio_capture.c/.h` | I2S stereo RX via DMA |
| `audio_gate.c/.h` | `stop_audio` gate (volatile bool) |
| `vad.c/.h` | Single-mic energy VAD + hangover |
| `mic_dsp.c/.h` | NLMS canceller + dual-channel ratio (host-tested, runtime bypassed) |
| `opus_stream.c/.h` | libopus encoder (Spike-1 params) |
| `ring_buffer.c/.h` | 60s PSRAM history (portMUX spinlock) |
| `c6_packet.c/.h` | §C.6 writer (portable C, no ESP deps) |
| `ble_drain.c/.h` | Core-0 drainer: ring → §C.6 → notify; `drain_replay` |
| `ble_link.c/.h` | NimBLE GATT (audio notify / command write / ack notify) |
| `commands.c/.h` | §D verify (libsodium) + dispatch + dedupe + ack |
| `executor.c/.h` | Executor task (dispatches by `cmd_type_t`) |
| `executor_core.c/.h` | Parse/validate (mirrors server schemas) |
| `provisioning.c/.h` | BLE provisioning GATT service |
| `provisioning_core.c/.h` | Provisioning state machine + NVS |

Host tests (`test/`): `test_c6_packet.c` (golden bytes), `test_vad.c`,
`test_mic_dsp.c` (NLMS), `test_provisioning.c`, `test_executors.c`; `command_vector.md`
(§D golden signed frames); `Makefile` runs all five (`make` → ALL PASS).

### 2.2 Boot / init sequence

`app_main` (`openrecall_sensor.c`): `nvs_flash_init` → `provisioning_init()`
(loads NVS key, calls `commands_set_pubkey` if provisioned) → init `ring_buffer`,
`audio_capture`, `opus_stream`, `ble_link` (registers GATT services) →
`commands_init(provisioned_pubkey, ble_link_notify_ack)` → spawn tasks:
- `audio_task` pinned to **core 1** (capture → VAD → encode → ring), stack 32768.
- `ble_drain` task on **core 0** (ring → §C.6 → notify).
- `executor` task on **core 0** (queue depth 8, `EXECUTOR_TASK_STACK=4096`,
  `EXECUTOR_TASK_PRIO=5`, `config.h:143-146`).

Core/priority layout (`openrecall_sensor.c:9-11`): core 1 = audio (encode in
isolation from radio); core 0 = BLE drain + executor + NimBLE, all prio 5.
Splitting encode (core 1) from radio (core 0) is why Spike 1 measured the encoder
in isolation. NimBLE runs on core 0 by default.

### 2.3 Audio signal path

`audio_task` (`openrecall_sensor.c:51-200`):
1. `audio_capture_read_stereo(pri, ref)` (`openrecall_sensor.c:72`) — I2S stereo
   RX via DMA. Two INMP441 mics share the I2S bus: `I2S_BCK_GPIO=5`, `I2S_WS_GPIO=6`,
   `I2S_DATA_GPIO=4` (`config.h:126-128`). 32-bit slot / 16-bit data workaround
   (the INMP441 puts 24-bit data left-aligned in a 32-bit slot; the driver reads the
   upper 16 bits). `PRIMARY_CHANNEL=0` = left = voice mic, faces the mouth
   (`config.h:135`).
2. `audio_gate_paused()` (`openrecall_sensor.c:76`) — if `stop_audio` is in effect,
   drain DMA but push only a GAP_MARKER so the ring stays contiguous; skip
   energy/VAD/encode.
3. Single-mic energy VAD: `vad_process_single(&vad, pri, FRAME_SAMPLES)`
   (`openrecall_sensor.c:102`). The dual-channel ratio gate + NLMS canceller are
   intentionally NOT used (`openrecall_sensor.c:95-101`): two omnidirectional
   INMP441s with insufficient acoustic shadowing both hear the wearer's voice at
   ~equal level (ratio ~1), so the ratio gate would reject the voice and the
   canceller would adapt to subtract it. The reference channel is still captured
   for the per-second calibration log.
4. On `C6_SPEECH`/`C6_HANGOVER`: `opus_stream_encode(pri, opus_buf, ...)`
   (`openrecall_sensor.c:108`); if `n > 0 && n <= UINT8_MAX`, `ring_buffer_push`.
   On silence: push a zero-length GAP_MARKER so the ring keeps a contiguous record.
5. `rel_ts_ms` is a monotonic per-device ms counter (not wall clock — the device may
   not have synced time), starting at 0 at boot, advancing by `FRAME_MS` per frame
   (`openrecall_sensor.c:46-50`). The server uses it to lay out transcript windows.

### 2.4 Algorithms implemented in firmware

**Single-mic energy VAD** (`vad.c`): mean-square energy threshold
`VAD_ENERGY_THRESHOLD = 50000` (`config.h:56`) on the primary channel, with a
`VAD_HANGOVER_FRAMES = 30` (600 ms, `config.h:32`) keep-emitting-after-speech-ends
window and `VAD_PREROLL_FRAMES = 15` (300 ms, `config.h:33-34`) replay-this-much-
history-on-onset. Output states: `C6_GAP_MARKER`, `C6_SPEECH`, `C6_PREROLL`,
`C6_HANGOVER`. Host-tested in `test_vad.c`.

**Dual-channel ratio gate + NLMS canceller** (`mic_dsp.c`): `VAD_RATIO_THRESHOLD=4`
(`config.h:61`); `DSP_NLMS_TAPS=32`, `DSP_NLMS_STEP_Q15=6553` (~0.2),
`DSP_NLMS_LEAK_Q15=1`, `DSP_NLMS_EPS=512` (`config.h:69-72`). Host-tested in
`test_mic_dsp.c` but **runtime-bypassed** (omni mics give only ~1.5-2× separation).

**Opus encode** (`opus_stream.c`): libopus 1.5.2 fixed-point, 16 kHz mono, 20 ms
frames, 24 kbps, complexity 1. Spike 1 measured 6.0 ms/frame (~30% of core 1)
(`config.h:23`). Per-frame encoded cap `MAX_OPUS_BYTES=256` (fits §C.6 u8 len).

**PSRAM ring buffer** (`ring_buffer.c`): `RING_SECONDS=60` → `RING_FRAMES=3000`
(`config.h:27-28`), ~773 KB. Protected by a portMUX spinlock. **TOCTOU fix:**
`ring_buffer_get_copy` copies frames under the lock (the executor/replay path reads
the ring concurrently with the audio task writing it) — the older
`ring_buffer_get` returned a pointer into the ring, which the writer could invalidate
mid-read. `drain_replay` uses the copy path (`ble_drain.c:82`).

**§C.6 encoding** (`c6_packet.c`): portable C, no ESP deps; host-tested vs the
server encoder (see §8.1).

**BLE drain** (`ble_drain.c`): core-0 task. Live path: pack 50 frames/chunk into
MTU-sized §C.6 packets, emit `C6_LIVE`, advance chunk_seq. On VAD onset, replay
`VAD_PREROLL_FRAMES` of history first (preroll). Silent batches emit one empty
GAP_MARKER so chunk_seq keeps advancing (`ble_drain.c:273-283`).

**`drain_replay`** (`ble_drain.c:39-157`): emits the last `seconds` of ring-buffered
frames as `C6_MEMORY_CHUNK` packets, continuing the same monotonic chunk_seq the
live path uses, with `C6_FLAG_LAST_OF_REQ` on the final packet. Caps `N` to
`RING_FRAMES` (3000) and to available frames (`ble_drain.c:54-60`); copies under
`ring_buffer_get_copy`; suppresses silence gaps in the body; if the ring is empty
still emits one boundary packet with LAST_OF_REQ (`ble_drain.c:148-154`). Log:
`drain: replay done: seconds=%u frames=%u chunk_seq->%u`. The capped edge case logs
`drain: replay capped: requested 60 s, have %u frames`.

**§D verify + dispatch** (`commands.c`): libsodium
`crypto_sign_ed25519_verify_detached` (`commands.c:76`) against the provisioned
32-byte pubkey. On valid: `cJSON_ParseWithLength` → read `command_id` + `type` →
dedupe via a ring buffer of last 16 ids (`CMD_DEDUPE_N=16`, `commands.c:14,38-50`)
→ `executor_submit(type, params)`. On invalid: dropped, no ack. Idempotency:
re-seen command_ids are re-acked but NOT re-executed (`commands.c:94-95`).

**Executor framework** (`executor.c`, `executor_core.c`): core-0 queue
(`EXECUTOR_QUEUE_DEPTH=8`, SPSC). Dispatch by `cmd_type_t` (`executor_core.h:32-39`):
- `CMD_START_AUDIO` → `audio_gate_set(false)` (`executor.c:26-29`)
- `CMD_STOP_AUDIO` → `audio_gate_set(true)` (`executor.c:30-33`)
- `CMD_REQUEST_BUFFER` → sends `replay_request_t{seconds}` to the drain replay
  queue (`executor.c:34-45`)
- `CMD_CAPTURE_PHOTO`/`CMD_RECORD_VIDEO` → logged "not implemented (P4b)"
  (`executor.c:46-51`)
- `play_audio`/`display_text`/`show_status` → `EXEC_UNKNOWN_TYPE`
  (`executor_core.c:73-76`)

**`executor_core` parse/validate** (`executor_core.c`): mirrors the server's
`_TYPE_SCHEMAS` (`validator_command.py:78-108`) — defense-in-depth, both bound-check
`seconds ∈ [1,60]` and `duration_s ∈ [1,30]`. Host-tested in `test_executors.c`.

**audio_gate** (`audio_gate.c`): `volatile bool` paused flag; `audio_gate_set` /
`audio_gate_paused`. The `stop_audio` command flips it; the audio task checks it
each frame and pushes GAP_MARKERs instead of encoding.

**BLE provisioning** (`provisioning.c`, `provisioning_core.c`): separate GATT
service. `PROV_STATE_CHAR` (read+notify, 0/1), `PROV_KEY_CHAR` (write, 32 bytes),
`PROV_RESET_CHAR` (write, 4-byte magic `0xA5A5A5A5`). NVS namespace `sense_prov`,
keys `srvkey` (blob) + `provd` (u8). `provisioning_core_apply_key` writes NVS first
then calls `commands_set_pubkey` to update the live verifier key. Host-tested in
`test_provisioning.c`.

### 2.5 config.h — every define

Audio (`config.h:17-24`): `SAMPLE_RATE=16000`, `CHANNELS=1`, `FRAME_MS=20`,
`FRAME_SAMPLES=320`, `FRAME_BYTES=640`, `OPUS_BITRATE=24000`, `OPUS_COMPLEXITY=1`,
`MAX_OPUS_BYTES=256`.

Ring (`config.h:27-28`): `RING_SECONDS=60`, `RING_FRAMES=3000`.

VAD (`config.h:31-34,56`): `VAD_HANGOVER_MS=600` (30 frames), `VAD_PREROLL_MS=300`
(15 frames), `VAD_ENERGY_THRESHOLD=50000`, `VAD_RATIO_THRESHOLD=4` (`config.h:61`).

DSP NLMS (`config.h:69-72`): `DSP_NLMS_TAPS=32`, `DSP_NLMS_STEP_Q15=6553`,
`DSP_NLMS_LEAK_Q15=1`, `DSP_NLMS_EPS=512`.

§C.6 (`config.h:75-80`): `C6_VERSION=1`, `C6_HEADER_LEN=12`, `C6_FLAG_HISTORICAL=0x01`,
`C6_FLAG_LAST_OF_REQ=0x02`, `C6_FRAMES_PER_CHUNK=50`.

BLE UUIDs (`config.h:97-110`): main service `6e9d0001-...-0a10` + audio/command/ack
chars (0002/0003/0004); provisioning service `6e9d0010-...-0a10` + state/key/reset
chars (0011/0012/0013); `PROV_NVS_NAMESPACE="sense_prov"`, `PROV_FACTORY_RESET_MAGIC=0xA5A5A5A5`.

SD (`config.h:113-117`): `SD_PIN_SCK=7`, `SD_PIN_MISO=8`, `SD_PIN_MOSI=9`,
`SD_PIN_CS=21`, `SD_SPI_HZ=20000000` (P4b media).

I2S (`config.h:126-135`): `I2S_BCK_GPIO=5`, `I2S_WS_GPIO=6`, `I2S_DATA_GPIO=4`,
`PRIMARY_CHANNEL=0`.

Server pubkey (`config.h:137-140`): `SERVER_ED25519_PUBKEY[32] = {0}` placeholder
(all-zeros rejects everything until provisioned).

Executor (`config.h:143-153`): `EXECUTOR_TASK_STACK=4096`, `EXECUTOR_TASK_PRIO=5`,
`EXECUTOR_TASK_CORE=0`, `EXECUTOR_QUEUE_DEPTH=8`, `REQ_BUFFER_MIN_SECONDS=1`,
`REQ_BUFFER_MAX_SECONDS=60`, `DRAIN_REPLAY_QUEUE_DEPTH=4`.

### 2.6 Memory budget

~852 KB app, ~19% free at steady state. Audio task stack 32768; high-water mark
logged every 3000 frames (60 s) (`openrecall_sensor.c:127-134`). Ring 773 KB in
PSRAM. Dual-core split keeps the encoder off the radio core.

---

## 3. Tier 2 — Android Relay (`android/openrecall-relay`)

Single-module Compose app, package `com.openrecall.relay`, `applicationId` same,
`minSdk=26` (BLE 2M PHY + java.util.Base64 + modern GATT), `compileSdk=34`,
Kotlin 2.0.20, AGP 8.5.2. No Hilt/Koin/Room/Retrofit/WorkManager — manual DI by
design (`RepositoryModule.kt:31-35`). All UI is Jetpack Compose; source in
`kotlin/` not `java/`.

### 3.1 App structure

- `RelayService` (`RelayService.kt:59`) — foreground service, the bridge.
- `ble/SensorLink.kt` — BLE central role (GATT).
- `net/ServerSocket.kt`, `WsUrl.kt` — OkHttp WebSocket client + URL derivation.
- `protocol/Messages.kt` — §E (de)serialization, the relay brain's vocabulary.
- `RelaySession.kt` — pure I/O-free relay brain (audio forwarding, command re-wrap).
- `relay/` — `RelayController` state hub, `Backoff`, state sealed interfaces.
- `http/OpenRecallHttpClient.kt` + `http/dto/` — HTTP control API + DTOs/mappers.
- `data/` — repositories, `SpeakerCache`, `SpeakerActions`, `SpeakerLabels`.
- `setup/ProvisioningClient.kt`, `SetupViewModel.kt` — BLE provisioning wizard.
- `ui/` — Compose screens (chat, recordings, home, device, memory, commands,
  settings, atom detail) + design system.

`RepositoryModule` (`data/RepositoryModule.kt:37`) is a process-singleton `object`;
`init(app)` constructs a `Repositories` data class of 13 fields once
(`RepositoryModule.kt:48-73, 89-175`), including wiring
`SpeakerCacheSeeder.launchOnAuthenticated` to seed the speaker cache on every
`ServerState.Authenticated` transition (`RepositoryModule.kt:158-159`).

### 3.2 BLE central role — SensorLink

`ble/SensorLink.kt`. GATT UUIDs (`:42-46`) — all four match firmware exactly:
- `SERVICE 6e9d0001-...-0a10`, `AUDIO 6e9d0002`, `COMMAND 6e9d0003`, `ACK 6e9d0004`,
  `CCCD 00002902-...`.

**Scan** (`:67-73`): `ScanFilter` by SERVICE UUID (no device-name "OpenRecall"
filter), `SCAN_MODE_LOW_LATENCY`, no explicit scan timeout. **Connect** (`:104-105`):
`connectGatt(context, false, gattCallback, TRANSPORT_LE)` — `autoConnect=false`
(direct connect), `TRANSPORT_LE` (GATT profile id is silently dropped on many
Android 10 stacks). **MTU** (`:115`): `requestMtu(247)` on STATE_CONNECTED;
services discovered only after MTU negotiation (`:121-124`). **Subscribe**
(`:126-134`): gets service, stashes `commandChar`, subscribes to AUDIO then ACK
(order matters) via `subscribe` (`:154-162` — `setCharacteristicNotification(true)`,
`ENABLE_NOTIFICATION_VALUE`, writeDescriptor), enqueues `onConnected` after both
CCCD writes are queued.

**Audio reception** (`:144-151`): `onCharacteristicChanged` reads `c.value`,
dispatches by UUID: `AUDIO → listener.onAudio(value)`, `ACK → listener.onCommandAck(value)`.
Direct synchronous callback on the GATT thread — no channel/flow.

**Serial GATT op queue** (`:56-57, 166-177`): `opQueue: ArrayDeque<() -> Unit>` +
`opInFlight: Boolean`. Android GATT allows ONE outstanding op at a time, so all
writes (CCCD subscribes + command writes) serialize through `enqueue` → `drain` →
`nextOp`. `onCharacteristicWrite`/`onDescriptorWrite` call `nextOp`; status is
ignored (`:137-142`).

**Connection state machine (implicit):** Scanning → Connecting → Negotiating MTU →
Discovering services → Subscribing → Connected/Ready → Disconnected. No explicit
enum; readiness signaled by `onConnected()`. **No reconnection in SensorLink**
(`autoConnect=false`, `stop()` disconnects+close). Reconnection/backoff is
`Backoff` + `RelayService`.

### 3.3 WebSocket relay

`net/ServerSocket.kt` — thin OkHttp transport ("no protocol logic here", `:12-16`).
Single `OkHttpClient` (no read timeout, 20 s `pingInterval`). `connect()` opens WS
with a single `Authorization: Bearer $token` header (`:36`) — no query-param auth,
no session_id in URL. `onMessage` text → `listener.onText`; binary → empty ("server→
relay is text only", `:41`); both `onClosed` and `onFailure` collapse to `onClosed`
(`:42-45`).

`net/WsUrl.kt` — `wsGatewayUrl(serverUrl, gatewayPort)` (`:32`): scheme-swap
(https→wss), host via `java.net.URI`, port = `gatewayPort ?: uri.port.takeIf != -1`,
drops path/query. The HTTP API and WS gateway run on separate ports; a scheme-swap-
only URL keeps the HTTP port and fails the 101 upgrade, so the phone reads the WS
port from `/health` → `gatewayPort` (`WsUrl.kt:8-31`).

### 3.4 RelaySession — the pure relay brain

`RelaySession.kt:16-19` — pure, I/O-free. Constructor
`RelaySession(sessionId, startSeq=0, speakerCache=null)` (`:26-30`). Returns
`RelayAction`s (`:156-165`); never touches SensorLink/ServerSocket. `RelayService.execute`
(`:228-240`) interprets them.

**Hello handshake + audio hold-back** (`:44-61`): `start()` emits
`SendServerText(Hello(sessionId, startSeq).encode())`, then flushes `pendingAudio`
as `SendServerBinary` in arrival order. `onDeviceAudio(packet)`: if `started` →
forward verbatim; else enqueue `pendingAudio`. Rationale (`:33-39`): GATT thread can
deliver audio before the WS reader fires `onOpen` → `start`; ungated audio would
reach the server before `hello` and the server closes 1002 ("audio before hello").
OkHttp sends pre-`onOpen` frames FIFO, so `hello` must be first. `stop()` (`:140-144`):
`started=false`, clear `pendingAudio`, emit `Bye`. Resets the gate so re-`start` drops
stale audio.

**Commands: server → device** (`:64-82, 146-152`): `ServerMessage.Command` →
`forwardCommand` base64-decodes `cmd.sig` → raw 64 bytes, concatenates
`signature + payload.toByteArray(UTF_8)` → `WriteDeviceCommand(signature + payload)`.
Reconstructs the device on-wire frame `[64-byte sig][payload JSON]` — NOT verbatim.
`RequestChunks` → `Note("backfill not yet supported")` (stub, `:67-68`).
`Transcript` → upsert `speakerCache` → `Note`. `Ack` → empty (cursor ack).
`Proactive` → `forwardProactive`. `Unknown` → empty (forward-compat ignore).

**Commands: device → server** (`:132-135`): `onDeviceCommandAck(ackPayload)` →
`commandId = String(ackPayload, UTF_8)` → `CommandAck(sessionId, commandId).encode()`.

**Proactive / name-speaker** (`:93-129`): if `msg.propose != null` → `ChatMessage`
kind `NAME_SPEAKER` with `propose`; else `AGENT_PROACTIVE` with atoms mapped to
empty `AtomChip`s. Both → `ForwardToChatHistory(chatMessage)`.

### 3.5 RelayService — the foreground service

`RelayService.kt:59`. Notification channel `openrecall_relay` (`:63`), name
"OpenRecall Relay", `IMPORTANCE_LOW`; notification "Bridging wearable ↔ server",
`setOngoing(true)`, id 1 (`:310-323`). `START_STICKY` (`:151`).

`onStartCommand` (`:101-152`): reads `server_url`/`token`/`gateway_port` extras;
START_STICKY redelivery restores from `ServerConfig(filesDir).read()` via
`runBlocking` (`:113-118`). Bumps `@Volatile generation` (`:121`); per-call listeners
capture `gen` and early-return if stale (`:42-47, 156, 198`) — the only explicit
memory-visibility mechanism. Constructs fresh `RelaySession` + `SensorLink`,
publishes `BleScanning`, `sensor.start()`.

**SensorLink listener** (`:155-194`): `onConnected` derives WS URL, publishes
`BleConnected`, opens `ServerSocket`. `onAudio` → `session.onDeviceAudio`. `onCommandAck`
→ `session.onDeviceCommandAck`. `onDisconnected` → `softDrop`.

**Socket listener** (`:197-226`): `onOpen` resets `reconnectAttempt`, publishes
`Authenticated` + `Live(sessionId)`, `session.start()`. `onText` → `session.onServerMessage`.
`onClosed` → `Unreachable`, `softDrop`.

**execute** (`:228-240`): `SendServerBinary` → `socket.sendBinary`; `SendServerText`
→ `socket.sendText`; `WriteDeviceCommand` → `sensor.writeCommand`; `ForwardToChatHistory`
→ `chatHistoryStore.append`; `Note` → `Log.i`.

**Auto-reconnect** (`:254-298`): `softDrop(gen, reason)` stops session/socket/sensor
(NOT `stopSelf`) → `scheduleReconnect`. `scheduleReconnect`: if
`Backoff.shouldGiveUp` → `Failed`; else compute backoff, publish `Reconnecting`,
launch in `serviceScope`: `delay`, re-check generation, fresh `SensorLink`, re-scan.

### 3.6 Threading — three execution domains

1. **Main/service thread** — `onStartCommand`; `runBlocking` config restore (`:114`).
2. **BLE GATT thread** — `BluetoothGattCallback` (`SensorLink.kt:109-152`); all
   `SensorLink.Listener` callbacks run here. `RelayService` does no thread-marshaling —
   directly calls `session.onDeviceAudio` / `execute` on the GATT thread. `execute`
   writes to `socket.sendBinary` (OkHttp thread-safe), `sensor.writeCommand`
   (re-enters GATT queue), `chatHistoryStore.append`.
3. **OkHttp WebSocket reader thread** — `WebSocketListener` → `RelayService.socketListener`
   → `session.start` / `onServerMessage` / `execute`.

Cross-thread: no shared queue/channel. `RelaySession.started` + `pendingAudio` are
plain (non-volatile, non-locked) fields — the design assumes single GATT producer +
single OkHttp consumer with `start()` flush as handoff (`RelaySession.kt:40-41`).
`RelayController`'s `MutableStateFlow` is thread-safe for concurrent writes.
`chatHistoryStore` is a process-singleton `StateFlow`-backed store appended from
GATT/WS threads.

### 3.7 Speaker recognition UI

**SpeakerCache** (`data/SpeakerCache.kt`): `ConcurrentHashMap<String, SpeakerEntry>`
mirrored into `MutableStateFlow`. `SpeakerEntry(speakerId, name?, isWearer)` — no
biometrics. `upsert`/`get`/`snapshot`/`seed`/`remove`.

**SpeakerCacheSeeder** (`data/SpeakerCacheSeeder.kt`): `launchOnAuthenticated`
collects `serverStates.distinctUntilChanged().filter { it is Authenticated }`, on
each tick `runCatching { seedOnce() }` with `onFailure` logged (not thrown) so the
collector survives (`:44-60`). Triggered on first connect, reconnect, post-re-provision.

**Historical-session seeding** (`SessionDetailViewModel.kt:202-209`): for each
`TranscriptChunk` with non-null speaker, if `existing == null || existing.name == null`,
upsert. Never downgrades a real name to null. Populates the reassign picker for
recordings whose speakers never appeared on the live WS auth path.

**SpeakerLabels** (`data/SpeakerLabels.kt:13-18`) — "Person N" synthesis:
```kotlin
fun personLabels(speakersInOrder: List<SpeakerEntry>): Map<String, String> {
    var n = 0
    return speakersInOrder
        .filter { !it.isWearer && it.name == null }
        .associate { it.speakerId to "Person ${++n}" }
}
```
Pure, stateless, client-synthesized at render. Server keeps `display_name=null`.

**SpeakerActions** (`data/SpeakerActions.kt:12-15`) — the UI→server seam (HTTP-always;
no `SpeakerControlPort` exists — grep returned zero matches):
```kotlin
interface SpeakerActions {
    suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String)
    suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String = "all")
}
```
`HttpSpeakerActions` delegates to `SpeakerRepository` → `OpenRecallHttpClient.renameSpeaker`
(POST `/speakers/{id}/rename`) / `reassignSpeaker` (POST `/speakers/reassign`).
`sessionId` accepted for interface parity but ignored (v1 reassign is global, `:18-19`).

**Optimistic-cache + revert** (`SessionDetailViewModel.renameSpeaker`, `:156-170`):
capture prior → optimistic upsert → launch HTTP → on failure revert (prior!=null
re-upsert, else remove) + set `_speakerError = "Couldn't rename on the server"` → on
success clear error. `ChatViewModel.nameSpeaker` (`:215-233`) uses a different error
string ("Couldn't save the name on the server") and dispatches even when not live
(the RC3 fix — old gate silently dropped rename).

**You-confirmation nudge** (`SessionDetailViewModel.kt:58-62, 218-240`):
`YouConfirmationState`: Idle → Prompting(wearerId) → Done. `maybePromptYouConfirmation`
finds first `isWearer` TranscriptChunk, skips if already named or cache has real name.
`confirmYou` calls `renameSpeaker(wearerId, chosenName)`. Session-scoped.

### 3.8 HTTP client

`http/OpenRecallHttpClient.kt`: `class OpenRecallHttpClient(baseUrl, token, caPem?)`.
Bearer auth on every request. **Certificate pinning** via custom TrustManager (NOT
`CertificatePinner`): `pinnedTrustManager(caPem)` parses PEM, in-memory KeyStore,
alias `"openrecall-ca"`, `TrustManagerFactory` with that keystore (`:55-69`). Endpoints:
`/health`, `/provisioning/pubkey`, `/sessions`, `/sessions/{id}`, `/sessions/{id}/events`,
`/speakers`, `/speakers/{id}/rename`, `/speakers/reassign`, `/status`, `/agent`, `/memory`,
`/commands`. `HttpStatusException` (extends IOException) for non-2xx; agent/memory/command
APIs throw `HttpApiError` (RuntimeException) with `ErrorCode`.

`store/ServerConfig.kt` — Jetpack DataStore Preferences (NOT SharedPreferences). Token
stored **plaintext** (`:31`). Process singleton via `StoreHolder`.

### 3.9 Compose UI

Theme (`core/ui/`): `OpenRecallTheme` — monochrome by design, dynamic color OFF
(`OpenRecallTheme.kt:8-19`); warm graphite-on-ink accent reserved for live/recording
state. M3 `Typography()`, `Shape` (4/8/12/20/28 dp), `Dimens` (xs=4...xl=32, touch=48).
`ErrorMapper` (`core/ui/ErrorMapper.kt:18-28`): `ApiError.toDisplayMessage`.

Navigation (`ui/nav/`): `AppNavigation` Scaffold, bottomBar shown only for MAIN_ROUTES
(Home, Recordings, Chat, Settings). Routes: Home, Recordings, Device, Commands, Chat,
Memory, Settings, AtomDetail, SessionDetail. `SessionIdNavType` custom nav arg type.

Screens: Chat (`ui/chat/` — `ChatViewModel`, bubbles, input bar, thinking dots),
Recordings (`ui/recordings/` — pull-to-refresh, paged list, session detail with
speaker rename/reassign dropdowns + You-confirmation banner), Home (`ui/home/` —
dashboard fan-in), Device (`ui/device/` — diagnostic), Memory (`ui/memory/` — search
+ 150 ms debounce), Commands (`ui/commands/` — lifecycle-gated polling), Settings
(`ui/settings/` — masked token, reconfigure).

### 3.10 Algorithms / state machines (relay)

- **BLE connection state machine** (implicit, `SensorLink.kt`).
- **Serial GATT op queue** (`SensorLink.kt:56-57, 166-177`).
- **§E parsing** (`Messages.kt:92-123`) — never throws, JsonNull guard.
- **Audio hold-back / hello-gating** (`RelaySession.kt:44-61`).
- **Command frame reconstruction** (`RelaySession.kt:146-152`).
- **Reconnect backoff** (`Backoff.kt:15-29`): `STEPS = [1000, 2000, 4000, 8000, 30000]`
  (1→2→4→8→30 s cap), `MAX_ATTEMPTS=6`, no multiplier/jitter — fixed lookup table.
- **Reconnect state machine** (`RelayService.kt:254-298`).
- **Generation guard** (`RelayService.kt:81-82, 121, 156, 198`).
- **RelayConnectionState** (`RelayConnectionState.kt:19-39`): Idle, BleScanning,
  BleConnected, SocketConnecting, Live, Reconnecting, Failed.
- **Optimistic-cache + revert** (`SessionDetailViewModel.kt:156-170`).
- **personLabels** (`SpeakerLabels.kt:13-18`).
- **You-confirmation** (`SessionDetailViewModel.kt:58-62, 218-240`).
- **Paging** (`SessionRepositoryImpl.kt:52-154`): failure does not advance cursor so
  retry fetches same page.

### 3.11 Architectural invariant

`arch/ArchitecturalInvariantsTest.kt` — enforces ONE invariant (INV-11): `ui/` must
not import `com.openrecall.relay.http` or `.http.dto`. Allow-list: `SetupActivity.kt`
(tracked tech debt). Pure file-tree scan, runs in ms.

### 3.12 Tests

52 test classes (`app/src/test/`) + one androidTest (`RelayServiceStartTest.kt`).
Highlights: `RelaySessionTest` (executable spec), `MessagesParseTest` (JsonNull fix),
`SpeakerCacheSeederTest`, `SessionDetailViewModelTest` (optimistic rename + revert,
You-confirmation, cache seeding), `ChatViewModelTest` (nameSpeaker live-session-id read).

---

## 4. Tier 3 — Server (`server`)

Python aiohttp. Package `openrecall_server` (env vars `OPENRECALL_*`, internal app
keys `sense_*`). ~12k LOC, **801 tests across 106 files**.

### 4.1 Package layout (`src/openrecall_server/`)

- **`agent/`** (2,440 LOC) — cognitive read path: `context.py` (LLM persona),
  `planner.py`, `proactive.py`, `intent.py` (OpenAI-compatible LLM),
  `validator.py` + `validator_command.py`, `guardrails.py` + `guardrails_command.py`,
  `capability.py`, `audit.py`, `metrics.py`, `speaker_nudge.py`.
- **`commands/`** (697 LOC) — `model.py`, `signing.py`, `record.py`, `status.py`,
  `store.py`, `dispatcher.py`.
- **`contracts/`** (582 LOC) — `clock.py`, `id_generator.py`, `metrics.py`, `types.py`.
- **`events/`** (256 LOC) — `model.py` (CaptureEvent), `store.py`.
- **`gateway/`** (776 LOC) — `core.py` (GatewayCore + ProactiveOutbox), `adapter.py`
  (WS transport).
- **`http/`** (1,111 LOC) — `app.py`, `auth.py`, `token.py`, `routes/`.
- **`ingest/`** (1,664 LOC) — `audio_packet.py`, `opus_decoder.py`, `reassembler.py`,
  `pipeline.py`, `streaming_transcriber.py`, `whisper_streaming.py`, `transcriber.py`,
  `speaker_config.py`, `speaker_embedder.py`, `speaker_identifier.py`.
- **`memory/`** (2,998 LOC) — `atom.py`, `store.py`, `extract.py`, `llm.py`,
  `embeddings.py`, `extraction_worker.py`, `stages.py`, `index.py`, `retrieval.py`,
  `scoring.py`, `versioned.py`, `migrations.py`, `speaker_registry.py`.
- **`protocol/`** (121 LOC) — `messages.py` (§E).
- **`sessions/`** (368 LOC) — `index.py`, `lifecycle.py`.
- **`sim/`** (173 LOC) — `device.py`, `runner.py`, `opus_encoder.py`.
- **`vision/`** (164 LOC) — `model.py`, `pipeline.py`.
- **`media/`** (74 LOC) — `blob.py`.

Scripts: `run_gateway.py` (live entry), `run_device_sim.py`, `extract_memories.py`,
`query_memory.py`, `reextract.py`, `measure_rtf.py`, `smoke_streaming.py`.

### 4.2 The two servers in `run_gateway.py`

Two servers share one event loop via `asyncio.run(main_loop())` (`run_gateway.py:322-385`):
- **WS gateway** — `--port` default 8765. §C.6 binary frames carry audio; §E text
  frames carry control JSON. Driven by `serve()` (`run_gateway.py:364-379`).
- **HTTP control API** — `--http-port` default 8766. aiohttp `web.Application` via
  `build_app()` (`run_gateway.py:294-320`), started as `AppRunner`+`TCPSite`.

Startup prints (in order): HTTP control URL, WS URL, bearer token, server command
public key hex (`run_gateway.py:358-363`).

`build_app` (`http/app.py:26-94`): every dependency is a kwarg defaulting to `None`.
Stashed on `app[...]` under `sense_*` keys (`app.py:60-74`). Route modules added at
`app.py:85-93`; `commands` routes only if both command_store and command_dispatcher
present (`app.py:92-93`).

**Cold-start index rebuild** (`run_gateway.py:113-115`):
`SessionIndex.rebuild_from_store(store)` rebuilds the in-memory `/sessions` index
from the durable event store so a gateway restart doesn't 404 pre-restart sessions.

**Shared event loop** (`main_loop`): starts HTTP runner, `worker.start()`, registers
`proactive_engine.on_session_completion` and (if enabled) `SpeakerNudgeListener` as
worker listeners (`run_gateway.py:334,348`), then `serve(...)` runs forever.

### 4.3 Ingest pipeline

**Reassembly** (`ingest/reassembler.py`): `SessionReassembler`. `start_seq=0` → live
anchor at first received packet (`reassembler.py:39-45, 118-124`); `start_seq>0` →
resume, wait for anchor.

**Bug A — head-gap re-anchor timeout** (`reassembler.py:47-59, 78-112`): on reconnect
the relay resumes at a chunk_seq leaving an unfillable gap (BLE relay doesn't buffer
past audio). Without a deadline every future packet stalled forever.
`_maybe_skip_stale_gap()`: if a head gap stays open for `gap_timeout_ms` (default
**3000 ms** via `build_pipeline_factory(gap_timeout_ms=3000)`, `adapter.py:106`),
re-anchor at `min(self._buffer)`, drop the lost range, drain the now-contiguous head.

**C6 opcodes** (`ingest/audio_packet.py`): `PacketType` (LIVE=0, MEMORY_CHUNK=1,
HISTORICAL=2), `VadState` (GAP_MARKER=0, SPEECH=1, PREROLL=2, HANGOVER=3). Header
`struct "<BIIBBB"` (12 bytes). `is_last_of_request → flags & 0b10`. Strict `parse()`
(`audio_packet.py:89-135`).

**Streaming transcriber** (`ingest/streaming_transcriber.py`, `whisper_streaming.py`):
`StreamingTranscriber` keeps a rolling PCM buffer, `_committed_ms`. `feed(pcm)`
(`streaming_transcriber.py:162-221`): append, trim, call backend `transcribe`, convert
tokens to absolute time, **skip tokens with `absolute_start < self._committed_ms`**
(dedup across hop overlap), group new tokens into one Segment per hop, advance
`_committed_ms = segment.end_ms`. `streaming_from_tokens(backend, hop_ms=1000,
window_ms=5000)`.

`WhisperStreamingBackend` (`whisper_streaming.py:203-250`): default model
`mlx-community/whisper-large-v3-turbo`; `mlx_whisper.transcribe(..., word_timestamps=True,
no_speech_threshold=..., logprob_threshold=...)`. `_mlx_segments_to_tokens`
(`whisper_streaming.py:112-175`) applies:
- **Noise filter 1**: drop segment if `no_speech_prob > no_speech_threshold`.
- **Noise filter 2**: drop if `avg_logprob < logprob_threshold`.
- **Hallucination filter**: `_is_hallucinated_repetition` — `_HALLUC_MIN_RUN=5`
  consecutive identical tokens, OR `>=_HALLUC_MIN_TOKENS=6` tokens with
  `unique/total < _HALLUC_MAX_UNIQUE_RATIO=0.5` (`whisper_streaming.py:61-63, 72-101`).
- Aggregate guard drops all tokens if the whole response is repetitive
  (`whisper_streaming.py:168-174`).

Thresholds default `no_speech_threshold=0.6`, `logprob_threshold=-1.0`, threaded from
`agent_config.whisper` via `build_pipeline_factory` (`adapter.py:132-141`).

**Transcript emission with speaker_id** (`ingest/pipeline.py`):
`AudioIngestPipeline.ingest(packet)` (`pipeline.py:139-177`): reassemble → decode
frames → while buffer ≥ `hop_bytes` (1 s), slice hop, `segments = streamer.feed(pcm)`,
`spk = self._speaker(pcm)`, build `Transcript(text, duration_ms, speaker=spk.speaker_id,
speaker_confidence=..., speaker_assignment=...)`. Constants `DEFAULT_HOP_MS=1000`,
`DEFAULT_WINDOW_MS=5000`, `DEFAULT_SPEAKER_WINDOW_MS=2000` (`pipeline.py:45-46,54`).

### 4.4 Speaker recognition

**`build_speaker_identifier`** (`adapter.py:75-97`): returns `None` if
`not cfg.enabled` — structural rollout guard: zero embed calls, `speaker=None` on
every Transcript (`adapter.py:87-88`). `embedder=None` → `_select_embedder(cfg)`
(`adapter.py:54-72`): non-empty/non-"fake" `embed_model` → `ResemblyzerSpeakerEmbedder`;
else `FakeSpeakerEmbedder(dim=16)`.

**`SpeakerIdentifier`** (`ingest/speaker_identifier.py`): `identify(pcm, sample_rate)`
(`:113-121`): embed (swallow failure → None), then `_match`.

**`_match` — ring buffer + EMA + corroboration-before-mint** (`:123-147`):
- `>= confirm_threshold (0.78)` → `_confirm` (add to ring buffer, recompute EMA
  centroid, increment turn), clear tentative streak, return `confirmed`. **Only path
  that touches a centroid.**
- `[tentative 0.70, confirm)` → increment tentative streak; if `streak >= corroborate_n
  (3)` → `_confirm` + `confirmed`; else `tentative` — **a single tentative match never
  touches a centroid.**
- `< tentative` → clear streak, `_cluster(vec)`.

**`_cluster`** (`:165-200`): GC pending clusters older than `pending_ttl_s=60`; find
best pending by cosine; if none or `< cluster_threshold (0.65)` start a new pending
scratch cluster; else append + recompute pending centroid. **Corroboration**: if
`len(embeddings) >= corroborate_n (3)` AND `now - first_seen <= corroborate_window_s
(30)` → `_mint`.

**`_mint`** (`:202-219`): `registry.add_speaker`, add all embeddings as confirmed,
`recompute_centroid`, increment turns, `_apply_coldstart_enrollment`, return
`confirmed` with confidence 1.0.

**"You" enrollment** (`:223-253`): only the first minted speaker gets
`is_wearer`/`display_name="You"`/`enrollment_status="implicit"` IF no speaker is
already wearer, no other pending cluster has `>= corroborate_n-1` embeddings, and
either `len(speakers) <= 1` or the speaker is a clear turn-count leader. An
interleaved two-person conversation is never mis-attributed up front.

**`SpeakerRegistry`** (`memory/speaker_registry.py`): `Speaker` model (`:27-41`).
`_CentroidOps` (`:95-122`): `trim(rows)` drops `outlier_trim_pct=0.1` farthest
embeddings by cosine-from-mean; `ema_centroid(old, target) = alpha*target + (1-alpha)*old`
with `ema_alpha=0.05`. **InMemorySpeakerRegistry** (`:125-247`): dict + Lock; ring
buffer cap `ring_buffer_n=100`. **SqliteSpeakerRegistry** (`:250-438`):
`check_same_thread=False` + Lock; two tables (`speakers`, `speaker_embeddings`); cap
via `DELETE ... ORDER BY created_at DESC LIMIT -1 OFFSET ?`.

**`reassign_speaker`** (`:440-461`): for each session relabel events + atoms, move
embeddings, recompute both centroids (de-poisoning). v1 treats every scope as "all".

**Embedder backends** (`ingest/speaker_embedder.py`): `FakeSpeakerEmbedder` (sha256-
derived unit vector, dim=16). `ResemblyzerSpeakerEmbedder` (`:71-160`):
`_SUPPORTED_RATE=16000`, `_WARMUP_MS=1600` (the 1.6 s floor, `:92`). `_ensure_ready`
lazy-loads `resemblyzer.VoiceEncoder`, runs one dummy inference to **discover dim at
runtime** (`self._dim = int(vec.shape[0])`, `:134-141`). `embed` returns None if
`ms < max(min_speech_ms, _WARMUP_MS)`. `warmup()` at startup. Local-only in v1.

**2s rolling window decoupled from 1s hop** (`pipeline.py:109-128`): feeds the
embedder the last `speaker_window_ms=2000` ms of PCM (not the 1s hop), rolling by
dropping the head past `_speaker_window_bytes`. Ensures the embedder always sees
≥1.6 s, which a 1s hop could never reach — fixing the "every Transcript speaker=None"
bug.

**Thresholds + validator** (`ingest/speaker_config.py`): `confirm=0.78`,
`tentative=0.70`, `cluster=0.65`, `corroborate_n=3`, `corroborate_window_s=30`,
`pending_ttl_s=60`, `ring_buffer_n=100`, `outlier_trim_pct=0.1`, `ema_alpha=0.05`,
`coldstart_window_s=120`, `confirm_turns=10`, `name_nudge_turns=8`, `min_confidence=0.5`.
`_validate_bounds` (`:61-89`): `confirm > tentative`, and **`cluster < tentative`**
(`:75-81`) — the RC5 threshold-inversion fix: previously tentative 0.55 < cluster
0.65 absorbed distinct voices into existing speakers.

**`SpeakerNudgeListener`** (`agent/speaker_nudge.py`): listener on the extraction
worker. Confirm nudge (implicit "You" crosses `confirm_turns=10`) and Name nudge
(unnamed unknown crosses `name_nudge_turns=8` with `propose={"kind":"name_speaker",
"speaker_id":...}` and up to 3 sample transcript lines, `:85-102`). Dedupes per
speaker_id, respects `OPENRECALL_RATE_LIMIT_PER_MIN`. `set_ws_sender` rebinds per-
connection.

### 4.5 Memory extraction

**`ExtractionStage` 60s windowing** (`memory/stages.py`): `ExtractionStage(extractor,
clock, window_ms=60_000, version="v1")` (`stages.py:66-91`). `extract(session_id,
events, *, finalize=True) -> (atoms, consumed_seq)` (`stages.py:122-204`). `_windows`
(`:238-266`): each new window starts when `event.start_ms - window_start_ms >=
window_ms`. `_extracted_windows` (`:219-236`): if `finalize` return all windows; **else
`windows[:-1]`** — the live hold-back (`finalize=False` holds the trailing growing
window). For each window: `joined = " ".join(e.text for e in window)`,
`majority = _majority_speaker(window)`, extract once via LLM, build atoms
`atom_id=f"{last.event_id}:{index}"`, `consumed_seq = last.seq`.

**`LLMExtractor` + strict parser** (`memory/extract.py`): `DEFAULT_PROMPT`
(`:44-53`) forces ONLY a JSON array of `{"kind","text"}` objects (fact/task/preference/
event); forbids flat string arrays. `_parse` (`:114-166`): regex-extract `[…]`,
`json.loads`, must be a list, `[]` is the only valid empty, each item must be a dict
with `kind`+`text`. **Partial-success replies also raise `LLMParseError`** (no silent
drop, `:117-135`) so a model flipping format mid-session can't look like empty
extraction and advance the cursor.

**Dead-letter retry bounds** (`memory/extraction_worker.py`): `max_parse_failures=5`
(`:263,293`). On `LLMParseError` (`:559-611`): increment counter; if `>= max` →
dead-letter: `set_cursor(session_id, advance_to, extractor_version=...)`, pop counter,
`EXTRACTION_DEAD_LETTER_TOTAL`. Events stay in the store for re-ingestion after a
prompt/model fix. Counter in-memory, resets on success and restart.

**`EXTRACTOR_VERSION` cursor stamping**: `VersionStampStage` (`stages.py:269-283`).
The worker stamps the cursor with `self._extractor_version` on every `set_cursor`
(`extraction_worker.py:587-590, 628-631`). `get_cursor(session_id, extractor_version=...)`
(`store.py:258-271`): if `recorded_version != extractor_version` → returns `_NO_CURSOR=-1`
→ re-extract. Legacy cursors backfill `extractor_version='legacy'` via migration
(`migrations.py:64-85`), so any `v1` extractor re-extracts them — the stale-cursor fix.

**`ExtractionWorker`** (`memory/extraction_worker.py`): `ExtractionEnqueuer`
(`:69-236`): `asyncio.Queue` of `(session_id, finalize)`. `enqueue` (live,
`finalize=False`) vs `enqueue_finalize` (bye/close, `finalize=True`). `_enqueue`
(`:189-201`): **if `self._loop is not None` → `self._loop.call_soon_threadsafe(...)`**
— the cross-thread-safe path (NOT `asyncio.run`, which would block the worker thread).
`bind_loop(loop)` at `worker.start()` (`:729`).

**Listener dispatch** (`_dispatch_listeners`, `:327-446`): three-way precedence —
running loop → `create_task`; **bound loop → `call_soon_threadsafe`**; no loop →
`asyncio.run` (reconcile fallback). Never `asyncio.run` on the worker's own thread
for production dispatch.

**`process_session`** (`:477-675`): read versioned cursor, filter `pending =
[e for e in all_events if e.seq > cursor]`; compute `advance_to` BEFORE the raising
pipeline call; run pipeline; on `LLMParseError` dead-letter; on other exception
`INDEXING_FAILURES_TOTAL` + re-raise; on success `set_cursor`. **Empty-batch
contention fix**: `if indexed:` (`:653`) — only dispatch `SessionCompletion` to
listeners when new atoms were actually indexed, not merely when `pending` was
non-empty.

**`_run` loop** (`:753-785`): `asyncio.wait({get_task, stop_task},
return_when=FIRST_COMPLETED)`; on get → `await asyncio.to_thread(self._safe_process,
...)` so embedder/indexer don't block the event loop. `start()` binds loop,
**reconcile-on-start**, creates run task. `stop()` sets event, `wait_for(task, 5.0)`.

### 4.6 Memory retrieval + indexing

**`SqliteMemoryIndex`** (`memory/index.py`): `cosine` (`:32-39`); `add(atom, vector)`
`INSERT OR IGNORE` (`:143-161`); `search(session_id, query, k)` brute-force cosine in
Python (`:170-198`). `check_same_thread=False` + Lock.

**`EmbeddingStage`** (`stages.py:286-303`): `run(atoms)` single batch, all-or-nothing.
`OpenAICompatibleEmbedder.from_env` (`embeddings.py:39-48`): `OPENRECALL_EMBED_MODEL`
required, restores input order via `item["index"]`.

**`IndexingStage`** (`stages.py:306-324`): `index.add(atom, vector)` idempotent on
`atom_id`.

**`Retriever` (canonical)** (`memory/retrieval.py:63-141`): `retrieve(ctx)`: `query_vec
= embedder.embed([query])[0]`; `candidates = index.search(session_id, query_vec,
limit*4)` (4× over-fetch); score each via `scorer.score(query_vec, sr.vector, age_s)`;
skip `score <= 0`; deterministic sort by `(-score, -created_at.timestamp(), atom_id)`;
truncate to limit; returns `RetrievedContext` with audit metadata. Read-only (INV-6),
deterministic (INV-7). (`MemoryRetriever` is DEPRECATED — emits `DeprecationWarning`,
`retrieval.py:154-158`.)

**`SimRecencyScorer`** (`memory/scoring.py`): `half_life_s=7*24*3600` (7-day half-life,
`:57-60`). `score = cos * exp(-ln2 * age_s / half_life_s)` (`:68`) — exponential decay;
age=half_life → 0.5.

### 4.7 The agent / proactive engine

**`ContextBuilder` persona** (`agent/context.py`): opens (`:21-25`): "You are
OpenRecall, the user's ambient memory agent. You answer questions about what the
user has said, heard, and done, using only the retrieved memory atoms provided
below. You do not invent or assume beyond what those atoms say." The full
`_V2_SYSTEM_PROMPT` (`:21-72`) defines behavior rules, the 5 valid command types,
idempotency_key, citation rules, output JSON format. `system_prompt_version="v2"`,
`context_builder_version="v1"`.

**`Planner`** (`agent/planner.py`): stateless (INV-1). `plan(ctx)` (`:114-254`):
(1) RETRIEVE via `self._retriever.retrieve(...)` (note: the empty-retrieval
short-circuit was deliberately removed at `:126-147` — cold device-action requests
must reach the LLM); (2) BUILD CONTEXT; (3) REASON `await self._llm.reason(prompt)`
(async); (4) VALIDATE via `StrictJSONValidator`; (5) GUARDRAILS via
`ConfidenceGateGuardrails.decide`; (6) DISPATCH or ANSWER. For `ISSUE_COMMAND`: if
`isinstance(ctx.trigger, Proactive)` → REFUSE `PROACTIVE_TRIGGER_CANNOT_ISSUE_COMMAND`
(`:211-227`) — the single enforced prohibition; else `_dispatch_command` (`:256-399`):
validate → per-call `StrictCommandGuardrails` → `dispatcher.issue(...)` →
`outcome=ISSUE_COMMAND, command_id, command_status="PENDING"`. Confidence band:
`>=0.85 high, >=0.60 medium, else low` (`:507-512`).

**Proactive triggers** (`agent/proactive.py`): `ProactiveTriggerEngine` (`:83-197`)
is a listener on the extraction worker. `on_session_completion` (`:116-196`): builds
`PlannerContext` with `Proactive(trigger, transcript="")`; `await asyncio.wait_for(
self._planner.plan(ctx), timeout=self._plan_timeout_s)`; on RETURN →
`ws_sender.send_proactive(...)`; on REFUSE → `PROACTIVE_REFUSED_TOTAL`. Never re-raises.

**`proactive_plan_timeout` 2s→8s** (`proactive.py:33`): `DEFAULT_PLAN_TIMEOUT_S = 8.0`.
The old hard-coded 2.0 s was an SLA, not model-grounded: a cold local 7B loads in
~2.1 s, so every cold proactive call timed out. `plan_timeout_from_env` reads
`OPENRECALL_PROACTIVE_PLAN_TIMEOUT_S`, validates `> 0` (`:37-60`).

**`ProactiveOutbox` + hot-spin fix** (`gateway/core.py:63-145`): per-session bucket of
`ProactiveMessage`. `enqueue` sets `self._event`. `drain_all()` (`:114-138`) **clears
the event BEFORE draining** (`:134`) — so a bare `signal()` (e.g. connection-close
wake) with nothing to drain doesn't leave the event set, which would make `wait()`
return immediately forever and hot-spin at 100% CPU starving the event loop — the
"works ~20 s then every connection times out, no error in terminal" incident.
Connection-close uses `task.cancel()` (NOT `signal()`) to avoid a thundering-herd poke
(`adapter.py:351-365`).

**Rate-limit guardrails** (`agent/guardrails.py:45-145`): `confidence_autonomous=0.85`,
`confidence_confirm=0.60`, `rate_limit_per_min=20`. `decide`: rejection → REFUSE;
NO_MEMORY → REFUSE `NO_SUPPORTING_MEMORY`; rate limit (process-wide `sid="global"`,
deque drops >60 s) → REFUSE `RATE_LIMIT`; `>=0.85` RETURN; `>=0.60`
RETURN_WITH_UNCERTAINTY; else REFUSE `NOT_AUTONOMOUS`.

### 4.8 Command orchestration

**`CommandDispatcher`** (`commands/dispatcher.py:38-176`): `issue(command) ->
SignedCommand` (`:60-99`) — idempotency: if `idempotency_key` set and existing command
unacked → return original; sign; store PENDING + initial history; save. `ack`
idempotent (separate from state machine). `transition` enforces `is_valid_transition`.
`pending()` returns unacked + unexpired, in issue order (`:169-176`) — how commands
reach the relay→device: the transport polls `pending()` and re-offers until acked;
the device executes each command_id at most once (at-least-once, idempotent).

**Ed25519 signing** (`commands/signing.py`): `CommandSigner.sign(command) =
sign(command.canonical_bytes())`. `to_wire() = {"payload": <canonical str>, "sig":
<base64>}`. `verify_command(signed, public_key_bytes)`. `load_or_create_signer(path)`
(`:91-108`): stable server identity; raw 32-byte private key, chmod 0o600.

**Server pubkey provisioning**: `signer.public_key_bytes` (raw 32 bytes) printed as
hex at startup (`run_gateway.py:362-363`); `GET /provisioning/pubkey` exposes it
(`http/routes/provisioning.py:22-27`). The device verifies every inbound command
against this key and drops/never-acks forgeries.

**§D command wire format**: `CommandMessage` (`protocol/messages.py:95-104`):
`{"type":"command","session_id":...,"payload":...,"sig":...}`. The relay forwards
`payload`/`sig` verbatim; the device verifies before executing.

**Device command allowlist**: `StrictCommandValidator.ALLOWLIST` (`validator_command.py:44-50`)
= `{capture_photo, record_video, start_audio, stop_audio, request_buffer}`. Per-type
param schemas (`:78-108`).

**How commands reach relay→device**: `GatewayCore._pending_commands()` (`core.py:284-292`)
emits `CommandMessage` for each `dispatcher.pending()` matching this session. Delivered
on `_on_hello` (after the initial ack, `core.py:276`) and after each `_on_command_ack`
(`core.py:278-282`).

**`CommandStatus` state machine** (`commands/status.py`): PENDING, VALIDATED, ISSUED,
DELIVERED, EXECUTING, COMPLETED, FAILED, CANCELLED, TIMED_OUT; forward-only; terminal =
{COMPLETED, FAILED, CANCELLED, TIMED_OUT}.

### 4.9 HTTP control API routes

All routes go through `bearer_auth_middleware` (`http/auth.py:7-16`): reads
`app["sense_token"]`; if `None` → auth disabled; else requires `Authorization: Bearer
<token>` with `constant_time_eq` (`http/token.py:7-14`), 401 on mismatch.

| Method | Path | Status codes |
|---|---|---|
| GET | `/health` | 200 (advertises `gatewayPort`) |
| GET | `/provisioning/pubkey` | 200, 503 |
| GET | `/sessions` | 200, 400, 500 |
| GET | `/sessions/{id}` | 200, 404, 500 |
| GET | `/sessions/{id}/events` | 200, 404, 500 |
| GET | `/sessions/{id}/memory` | 200, 404 |
| GET | `/status` | 200 |
| POST | `/agent` | 200, 400 |
| GET | `/memory` | 200, 400 |
| GET | `/metrics` | 200 (text/plain) |
| GET | `/commands` | 200, 500 |
| GET | `/commands/{id}` | 200, 404, 500 |
| POST | `/commands/{id}/ack` | 200, 404, 409, 500 |
| GET | `/speakers` | 200 (`{"speakers":[]}` when disabled) |
| POST | `/speakers/{id}/rename` | 200, 400, 404, 409 |
| POST | `/speakers/reassign` | 204, 400, 404, 409 |

Speaker wire shape (`speakers.py:25-35`) carries no biometrics (centroid/
embedding_model/dim). The `rename_speaker` route (`speakers.py:66-83`) uses a
`reg.get(id) is None` guard (NOT `reg.name()` raising KeyError — `SqliteSpeakerRegistry.name()`
is a silent UPDATE that no-ops on a missing row, so relying on KeyError 500s on the
real backend).

### 4.10 Storage

Four SQLite stores, all `check_same_thread=False` + `threading.Lock` (the proactive
listener path runs `search` on a different thread):
- **`events.db`** — `SqliteEventStore`: `capture_events` table, `INSERT OR IGNORE`
  dedup by event_id; `last_event(session_id)` (highest seq) used by `_on_hello` to
  resume event_seq; `relabel_speaker`.
- **`atoms.db`** — `SqliteAtomStore`: `memory_atoms` + `extraction_cursor` (PK
  session_id, `last_seq`, `extractor_version DEFAULT 'legacy'`). Versioned cursor.
- **`memory_index.db`** — `SqliteMemoryIndex`: `memory_index` (atom_id PK, vector
  TEXT).
- **`speakers.db`** — `SqliteSpeakerRegistry`: `speakers` + `speaker_embeddings`.
- (Plus `commands.db` — `SqliteCommandStore`.)

`SessionIndex.rebuild_from_store` (`sessions/index.py:176-217`): wipes and replays
all events at cold start. `SessionLifecycle` (`sessions/lifecycle.py:20-54`):
thread-safe open-session set; `__len__` backs `/status` `activeSessions`.

### 4.11 Reconnect handling — the 3 bugs fixed

**Bug A — reassembler head-gap stall → 3s re-anchor** (`reassembler.py:47-59,
78-112`): on reconnect the relay resumes at a chunk_seq leaving an unfillable head
gap; without a deadline every future packet stalled forever. Fix: `gap_timeout_ms=3000`
+ monotonic clock; after 3 s re-anchor at the lowest buffered packet, drop the lost
range.

**Bug B — `_on_hello` reset `_event_seq=0` → event_id collisions** (`core.py:248-276`):
the BLE relay does NOT replay on reconnect — it resumes at the device's current
chunk_seq, so post-reconnect audio is NEW content. Resetting `_event_seq=0` made
every post-reconnect transcript collide with an existing `"{session}:{seq}"` event_id
and be dropped as duplicate, so it was never enqueued for extraction and never became
a memory ("no memory after reconnect"). Fix: `prior = self._store.last_event(...)`;
if prior → `_event_seq = prior.seq + 1`, `_cum_ms = prior.start_ms + prior.duration_ms`;
continue from last_event (`core.py:261-272`).

**Bug C — no finalize on bye/disconnect → trailing 60s window held forever**
(`core.py:294-313, 315-336`; `adapter.py:343-350`): the live extraction path holds the
trailing still-growing 60s window back (`finalize=False`). A relay disconnect without
a clean bye left it held forever. Fix: `_on_bye` calls `enqueue_finalize`; the
adapter's `finally` calls `core.finalize_pending_session()` (no-op if already bye'd
or never hello'd).

### 4.12 Config — every `OPENRECALL_*` env var group

**LLM** (`memory/llm.py:35-46`): `OPENRECALL_LLM_MODEL` (required), `_BASE_URL`
(default `http://localhost:11434/v1`), `_API_KEY`, `_TEMPERATURE` (default 0.2).

**EMBED** (`memory/embeddings.py:39-48`): `OPENRECALL_EMBED_MODEL` (required),
`_BASE_URL` (default `http://localhost:11434/v1`), `_API_KEY`.

**VLM** (`vision/model.py:47-56`): `OPENRECALL_VLM_MODEL` (required), `_BASE_URL`,
`_API_KEY`.

**SPEAKER** (`ingest/speaker_config.py`): `OPENRECALL_SPEAKER_ENABLED` (bool, default
false), `_EMBED_MODEL/BASE_URL/API_KEY`; floats `_{CONFIRM_THRESHOLD=0.78,
TENTATIVE_THRESHOLD=0.70, CLUSTER_THRESHOLD=0.65, EMA_ALPHA=0.05,
OUTLIER_TRIM_PCT=0.1, MIN_CONFIDENCE=0.5}`; ints `_{MIN_SPEECH_MS=500, CORROBORATE_N=3,
CORROBORATE_WINDOW_S=30, PENDING_TTL_S=60, RING_BUFFER_N=100, COLDSTART_WINDOW_S=120,
CONFIRM_TURNS=10, NAME_NUDGE_TURNS=8}`.

**Agent** (`agent/config.py`): `OPENRECALL_CONFIDENCE_AUTONOMOUS` (default 0.85),
`_CONFIRM` (default 0.60), `OPENRECALL_RATE_LIMIT_PER_MIN` (default 20),
`OPENRECALL_WHISPER_NO_SPEECH_THRESHOLD` (default 0.6), `_LOGPROB_THRESHOLD`
(default -1.0), `OPENRECALL_PROACTIVE_PLAN_TIMEOUT_S` (default 8.0). All validated at
startup — a bad value aborts with a clear error.

**LOG**: `OPENRECALL_LOG_LEVEL` (default INFO). **TOKEN_FILE**: default
`data/server_token` (`secrets.token_hex(32)`, 0600). **KEY_FILE**: default
`data/server_ed25519.key`.

### 4.13 Device simulator

`sim/device.py:DeviceClient` (`:23-87`) is the reference wearable/relay in Python —
the executable spec for the firmware C + Android Kotlin. Outbound: `hello`, `bye`,
`next_audio_packet`. Inbound: `on_message` handles `transcript` and `command` →
`_on_command` which **verifies the Ed25519 signature before acking** (`:77-87`);
forgeries dropped + counted, never acked. `scripts/run_device_sim.py` CLI: `--uri`,
`--session`, `--server-key` (hex, required), `--seconds`, `--frames-per-packet`,
`--idle-timeout` (30 s, generous for MLX cold-start), `--token`.

### 4.14 Test suite shape

801 tests across 106 files. Key integration tests:
- `tests/integration/test_cognitive_read_path.py` — G1–I8 (end-to-end
  event→atom→answerable, provenance, confidence band, empty retrieval refuses, audit,
  metrics, schema_version, latency, concurrency, bad bearer rejected).
- `tests/integration/test_command_issue.py` — G8–G12 (issue_command flows, idempotency
  dedup, capability/schema failure doesn't dispatch, dispatch + refusal audited).
- `tests/gateway/test_streaming_e2e.py` — real Opus decode + MLX streaming transcribe.
- `tests/gateway/test_proactive_outbox.py:91-127` — hot-spin regression.
- `tests/gateway/test_gateway_commands.py:66-94` — hello delivers pending commands.
- `tests/gateway/test_speaker_rollout.py` — disabled/enabled config, embedder selection.
- `tests/gateway/test_extraction_wiring.py:112-152` — `enqueue_finalize` extracts
  trailing window (Bug C).
- `tests/ingest/test_streaming_hop.py` — hop dedup.
- `tests/ingest/test_speaker_reassign.py` — relabel + move embeddings.
- `tests/memory/test_extraction_worker_listeners.py` — empty-batch contention,
  worker-thread listener blocking, no-loop fallback.
- `tests/sim/test_e2e.py:51` — full bidirectional contract over a real WebSocket with
  real Ed25519 crypto.
- `tests/http/test_speakers.py` — rename/reassign incl. Sqlite-backed 404 regression.

---

## 5. End-to-end: how it all comes together

### 5.1 Audio → transcript → memory (the live path)

1. **Firmware** (`audio_task`, core 1): I2S stereo read → `audio_gate_paused()`
   check → single-mic energy VAD → Opus encode → `ring_buffer_push`. `rel_ts_ms`
   advances per frame.
2. **Firmware** (`ble_drain`, core 0): pack 50 frames/chunk into MTU-sized §C.6
   packets, emit `C6_LIVE` notifications on AUDIO char, advance chunk_seq. On VAD
   onset, replay preroll first.
3. **Relay**: `SensorLink.onAudio(bytes)` (GATT thread) → `RelaySession.onDeviceAudio`
   → `SendServerBinary(verbatim)` → `socket.sendBinary` (OkHttp) → WS binary frame.
4. **Server**: `adapter.handle_message` → `core.on_audio` → `AudioPacket.parse`
   (`<BIIBBB`) → `SessionReassembler.accept` (order by chunk_seq, gap-skip, 3 s
   re-anchor) → `OpusStreamDecoder.decode` → PCM 16-bit LE @16 kHz.
5. **Server**: `StreamingTranscriber.feed` (rolling 5 s window, 1 s hop, mlx-whisper,
   token dedup via `_committed_ms`, noise + hallucination filters) → Segments.
6. **Server**: `SpeakerIdentifier.identify` (rolling 2 s speaker window, Resemblyzer,
   corroboration-before-mint, cold-start "You") → `spk.speaker_id`/`confidence`/
   `assignment`.
7. **Server**: `Transcript` → `_emit` → `CaptureEvent` (`event_id="{sess}:{seq}"`)
   → `EventStore.append` (INSERT OR IGNORE dedup) → `SessionIndex.record` →
   `ExtractionEnqueuer.enqueue` (finalize=False).
8. **Server** (`ExtractionWorker`): `process_session` → Pipeline.run:
   `ExtractionStage` (60 s windows, join text, LLM once/window, hold back trailing
   window) → `VersionStampStage` → `EmbeddingStage` → `IndexingStage` (atoms stored).
9. **Server** (on new atoms only): `ProactiveTriggerEngine.on_session_completion` →
   `Planner.plan` (RETRIEVE→REASON→VALIDATE→GUARDRAILS) → RETURN → `ProactiveMessage`
   sent as §E text frame; or REFUSE (counted).
10. **Relay**: `ServerMessage.Proactive` → `RelaySession.forwardProactive` →
    `ForwardToChatHistory(ChatMessage)` → UI renders (`NAME_SPEAKER` bubble if
    `propose`, else `AGENT_PROACTIVE`).

### 5.2 Command round-trip (server → device → ack)

1. **Server decides + signs**: `Planner.plan` → LLM returns `kind:"issue_command"`
   → (only `UserRequest` triggers may dispatch; `Proactive` forbidden) →
   `_dispatch_command` → `StrictCommandValidator` (allowlist + param bounds) →
   `StrictCommandGuardrails` (capability + resource + confidence ≥0.85) →
   `CommandDispatcher.issue` (idempotency dedup) → `CommandSigner.sign` (Ed25519
   canonical_bytes) → status PENDING → `SqliteCommandStore.save`.
2. **Server wraps as §E + sends down WS**: `GatewayCore._pending_commands` emits
   `CommandMessage(type:"command", session_id, payload, sig)` on `_on_hello` (after
   `Ack`) and after each `_on_command_ack`. Sent as WS text frame.
3. **Relay receives + re-wraps for BLE**: `RelaySession.forwardCommand` decodes base64
   `sig` → 64 raw bytes, concatenates `signature + payload.toByteArray(UTF_8)` →
   `WriteDeviceCommand` → `sensor.writeCommand` → BLE write on COMMAND char.
4. **Device verifies + executes**: `commands_handle` →
   `crypto_sign_ed25519_verify_detached` against provisioned pubkey → valid: cJSON
   parse → dedupe by command_id (ring of 16) → `executor_submit` → executor task
   dispatches by `cmd_type_t`. Invalid: dropped, no ack.
5. **Device acks via BLE**: `ack(command_id)` → `ble_link_notify_ack` → BLE notify on
   ACK char (raw command_id bytes).
6. **Relay wraps device ack as §E**: `SensorLink.onCommandAck` →
   `RelaySession.onDeviceCommandAck` → `CommandAck(sessionId, commandId).encode()` →
   WS text frame.
7. **Server reconciles**: `_on_command_ack` → `dispatcher.ack(command_id)` (idempotent
   set add) → returns `list(self._pending_commands())` (pulls next outstanding command).

**Trust boundary:** the signed §D command envelope is end-to-end (server→device).
The relay re-wraps (`sig+payload`) but cannot forge — the device verifies with
libsodium against the provisioned 32-byte pubkey. The bearer token guards only the
HTTP/WS transport, not command integrity.

**Data boundary:** §C.6 audio is forwarded **verbatim** by the relay (raw BLE
notification bytes → WS binary frame, no framing/wrapping). §E control is text-JSON,
relay-owned (relay adds `session_id`/`type` tags, translates device acks to
`command_ack`).

### 5.3 Speaker rename (HTTP-always)

1. User taps a speaker tag in SessionDetail → rename dialog → "Sarah".
2. `SessionDetailViewModel.renameSpeaker` (`:156-170`): optimistic cache upsert →
   `runCatching { speakerActions.nameSpeaker(...) }` → on failure revert cache +
   "Couldn't rename on the server"; on success clear error.
3. `HttpSpeakerActions.nameSpeaker` → `SpeakerRepository.renameSpeaker` →
   `OpenRecallHttpClient.renameSpeaker` → `POST /speakers/{id}/rename`.
4. Server `rename_speaker` (`speakers.py:66-83`): `reg.get(id) is None` → 404;
   `reg.name(id, dto.name)` → `{"speaker": _speaker_to_wire(reg.get(id))}` (no
   biometrics).
5. The same `speaker_registry` is shared between the WS gateway (mints speakers) and
   the HTTP API (renames) — both wired from `run_gateway.py:319,376`. (This wiring was
   Bug 1 of the rename fix; `run_gateway.py:312-318` documents it.)

### 5.4 Provisioning

- Server generates Ed25519 keypair (`load_or_create_signer`) + bearer token
  (`load_or_create_token`), prints both at startup.
- Two paths to get the pubkey onto the device:
  - **Compile-time** (`config.h:137-140`): paste hex into `SERVER_ED25519_PUBKEY[32]`.
  - **Runtime BLE provisioning**: separate GATT service (`config.h:102-110`): write 32
    bytes to PROV_KEY_CHAR → `provisioning_core_apply_key` (NVS-write-first) →
    `commands_set_pubkey` updates live verifier → STATE char notifies 1. Android:
    `SetupActivity` fetches pubkey via `GET /provisioning/pubkey`, writes via
    `BleProvisioning.writeServerKey`.
- Bearer token: entered in SetupActivity, persisted in DataStore, attached as
  `Authorization: Bearer $token` on both WS and HTTP.

---

## 6. Cross-cutting: every algorithm implemented

### 6.1 Firmware algorithms
- I2S stereo RX via DMA (32-bit slot / 16-bit data workaround); single primary channel
- Single-mic energy VAD (threshold 50000, 30-frame hangover, 15-frame preroll)
- Dual-channel ratio gate + NLMS canceller (host-tested, runtime-bypassed)
- Opus encode (libopus 1.5.2 fixed-point, 16 kHz mono 24 kbps complexity 1)
- PSRAM ring buffer (3000 frames/60 s, portMUX spinlock, TOCTOU-safe `get_copy`)
- §C.6 encoding (portable C)
- BLE drain + VAD preroll + drain_replay (MEMORY_CHUNK + LAST_OF_REQ)
- §D Ed25519 verify (libsodium) + dedupe (ring of 16) + dispatch + ack
- Executor framework (core-0 queue depth 8) + parse/validate mirroring server schemas
- audio_gate (volatile bool)
- BLE provisioning state machine + NVS persistence

### 6.2 Relay algorithms
- BLE scan (service-UUID filter) → connect (TRANSPORT_LE) → MTU 247 → discover → subscribe
- Serial GATT op queue (one outstanding op)
- §E parsing (never throws, JsonNull→Kotlin null fix)
- Audio hold-back / hello-gating (GATT-before-OkHttp race)
- Command frame reconstruction (base64 sig + payload → BLE write)
- Reconnect backoff ([1,2,4,8,30] s, 6 attempts, no jitter)
- Generation guard (volatile, per-call listener capture)
- RelayController state hub (StateFlow, single-writer, revision bump)
- Optimistic-cache + revert-on-failure (rename)
- personLabels "Person N" synthesis (pure, stateless, first-appearance order)
- You-confirmation state machine
- Paging (failure doesn't advance cursor)
- Lifecycle-gated status polling
- Certificate pinning (custom TrustManager, alias `openrecall-ca`)

### 6.3 Server algorithms
- §C.6 packet parse/encode + opcodes/VadState
- Opus decode (stateful per session)
- Reassembly with live-anchor + gap re-anchor (3 s timeout) — Bug A
- Rolling-window streaming transcription (hop dedup via committed cursor)
- Whisper noise + hallucination filtering (no_speech_prob, avg_logprob, repetition
  detector run≥5 / ≥6 tokens unique<0.5)
- Speaker ID: cosine match + corroboration-before-mint + EMA centroid + outlier trim
- "You" cold-start enrollment (turn-count leader, interleaved-conversation guard)
- Speaker reassign (relabel events+atoms, move embeddings, de-poison centroids)
- 60 s extraction windowing + live hold-back (`finalize=False` → `windows[:-1]`)
- Majority speaker attribution (confidence-weighted >50%)
- Strict LLM JSON parser (no silent drop, `LLMParseError`)
- Dead-letter (5 strikes → advance cursor, keep events)
- Versioned cursor (mismatch → re-extract; legacy migration)
- Cross-thread listener dispatch (`call_soon_threadsafe`, not `asyncio.run`)
- Reconcile-on-start
- Extraction run loop (FIRST_COMPLETED, `to_thread`)
- Proactive outbox hot-spin fix (clear-before-drain)
- Proactive plan timeout (2 s→8 s)
- Empty-batch contention gate (`if indexed:`)
- Retriever (4× over-fetch, sim×recency, deterministic sort)
- SimRecency scorer (cos × exp decay, 7-day half-life)
- Cosine similarity (4 implementations)
- Planner pipeline (retrieve→context→reason→validate→guard→dispatch)
- Confidence-gate guardrails (process-wide rate limit)
- Command guardrails (capability + resource + confidence ≥0.85)
- Strict command validation (allowlist + param bounds)
- Strict JSON validation (citation subset, confidence range)
- Ed25519 sign + verify + load_or_create (stable identity)
- Command idempotency (key + command_id dedup) + pending() re-offer
- Command status state machine (forward-only)
- Reconnect event_seq resume (Bug B) + finalize on bye/disconnect (Bug C)
- Bearer auth (constant-time)
- SessionIndex rebuild + cursor pagination
- All SQLite stores thread-safety (`check_same_thread=False` + Lock)

---

## 7. Testing & contracts — keeping both ends in sync without a shared library

There is no shared library. The firmware C encoder (`c6_packet.c`) and the server
Python encoder (`audio_packet.py`) are dual implementations kept in lockstep by:

1. The server-generated golden vector burned into the firmware C test
   (`test/test_c6_packet.c:GOLDEN`), regenerated from `AudioPacket(...).encode()`.
   `test_c6_packet.c:16-21` — same bytes the server test asserts.
2. The server test that independently asserts its encoder produces the same bytes
   (`tests/ingest/test_audio_packet.py:test_encode_matches_the_firmware_byte_layout`).
3. The header layout doc duplicated in both `c6_packet.h:8-15` and `audio_packet.py:2-16`.
4. The `command_vector.md` golden signed frames for bench testing the §D verify path.
5. The `executor_core.c` param validation mirroring the server's `_TYPE_SCHEMAS`
   (defense-in-depth, both bound-check `seconds∈[1,60]` and `duration_s∈[1,30]`).

Host tests (`firmware/openrecall_sensor/test/Makefile`): `make` → c6, vad, dsp,
provisioning, exec — ALL PASS.

---

## 8. Tiered bring-up runbook

Source: `docs/bring-up/2026-07-27-real-device-bringup.md`. Thesis: validate each tier
before the next so "a failure points at one layer, not three."

- **Tier 0 — Server baseline (no device)**: `run_gateway.py` + `run_device_sim.py`;
  pass = `capture_events` accumulates rows.
- **Tier 1 — Firmware audio path (device only)**: paste Tier 0 pubkey into `config.h`;
  `idf.py build && flash monitor`; pass = clean boot, audio task ~50 fps,
  `opus_bytes/s > 0` once subscribed, speech discriminates from ambient at wearable
  distance. Finding: at arm's length the INMP441 collapses — device must be worn.
- **Tier 2 — Full XIAO → Android → Mac path**: the NimBLE 1.6 assert smoke — first
  BLE GAP connect is the landmine the IDF 5.1.6 pin dodges; pass = speak →
  transcripts in gateway log → `capture_events` accumulates, **no
  `xQueueSemaphoreTake uxItemSize == 0` assert**.
- **Tier 3 — Speaker recognition on real audio**: `OPENRECALL_SPEAKER_ENABLED=true`;
  watch one voice → cold-start "You" → confirm nudge; second voice → corroboration →
  name nudge; mis-attribution → Reassign relabels.
- **Tier 4 — Command executors (P4a)**: `stop_audio` (opus_bytes/s=0, voiced holds
  cumulative); `start_audio` (resumes); `request_buffer seconds=5` (drain replay done
  log, MEMORY_CHUNK + LAST_OF_REQ, retrospective transcript); `request_buffer
  seconds=60` on fresh boot (capped log); `capture_photo`/`record_video` ("not
  implemented (P4b)").

---

## 9. The rebrands

### 9.1 Sense → OpenSapien (2026-08-08)

Historical record — the names below are the ones that existed at the time, and are
**not** live identifiers. The project was renamed from "Sense"/"AiSense" to
"OpenSapien" as one commit (615 files, +1639/-1639, `d9d39ab`):
- Server pkg `sense_server` → `opensapien_server`; pyproject `opensapien-server`.
- Android module `sense-relay` → `opensapien-relay`; package `com.sense.relay` →
  `com.opensapien.relay`; `SenseTheme`→`OpenSapienTheme`,
  `SenseHttpClient`→`OpenSapienHttpClient`; cert alias `sense-ca`→`opensapien-ca`;
  notification channel `sense_relay`→`opensapien_relay`.
- Firmware dir `sense_sensor` → `opensapien_sensor`; `sense_sensor.c`→
  `opensapien_sensor.c`; BLE device name "Sense"→"OpenSapien".
- Env vars `SENSE_*` → `OPENSAPIEN_*` (113 OPENSAPIEN_ refs, zero SENSE_).
- **Preserved**: hardware board name "XIAO ESP32S3 Sense" (Seeed), `SenseLog` Android
  log tag, filesystem path `/Users/kevin/Projects/Sense`, `esp32s3-sense` sdkconfig
  board refs, internal `sense_*` app keys, `sense_prov` NVS namespace.

History was rewritten locally (every commit → `kevinjacb
<kevingeniard2002@gmail.com>`, all `Co-Authored-By: Claude` trailers stripped — no
AI authorship visible on GitHub). Force-push to GitHub is the user's responsibility.

### 9.2 OpenSapien → OpenRecall

The current name. Every `OpenSapien`/`opensapien`/`OPENSAPIEN` token became
`OpenRecall`/`openrecall`/`OPENRECALL`, and the `Sense*` identifier prefix that
§9.1 had left behind was folded in as `Recall*`:
- Server pkg `opensapien_server` → `openrecall_server`; pyproject `openrecall-server`.
- Android module `opensapien-relay` → `openrecall-relay`; package
  `com.opensapien.relay` → `com.openrecall.relay`;
  `OpenSapienTheme`→`OpenRecallTheme`, `OpenSapienHttpClient`→`OpenRecallHttpClient`;
  cert alias `opensapien-ca`→`openrecall-ca`; notification channel
  `opensapien_relay`→`openrecall_relay`.
- Design system / logging prefix `Sense*` → `Recall*` (`SenseTheme`→`RecallTheme`,
  `SenseCard`→`RecallCard`, `SenseIcons`→`RecallIcons`, `SenseLog`→`RecallLog`,
  `SenseApplication`→`RecallApplication`, and the rest).
- Firmware dir `opensapien_sensor` → `openrecall_sensor`; `opensapien_sensor.c`→
  `openrecall_sensor.c`; BLE device name "OpenSapien"→"OpenRecall".
- Env vars `OPENSAPIEN_*` → `OPENRECALL_*`.
- **Preserved**: hardware board name "XIAO ESP32S3 Sense" (Seeed), `esp32s3-sense`
  sdkconfig board refs, internal `sense_*` app keys, `sense_prov` NVS namespace, and
  the frozen design comp under `android/openrecall-relay/design/`
  (`Sense Relay.dc.html`, `sense-device.js`) which is the original artifact.

---

## 10. Key files index (absolute)

**Firmware:**
- `firmware/openrecall_sensor/main/openrecall_sensor.c` — app entry, audio_task, core layout
- `firmware/openrecall_sensor/main/config.h` — every define
- `firmware/openrecall_sensor/main/c6_packet.c/.h` — §C.6 encoder
- `firmware/openrecall_sensor/main/ble_drain.c/.h` — drain task, drain_replay
- `firmware/openrecall_sensor/main/ble_link.c/.h` — NimBLE GATT
- `firmware/openrecall_sensor/main/commands.c/.h` — §D verify (libsodium) + dispatch
- `firmware/openrecall_sensor/main/executor.c/.h` + `executor_core.c/.h` — executor
- `firmware/openrecall_sensor/main/provisioning.c/.h` + `provisioning_core.c/.h` — BLE provisioning
- `firmware/openrecall_sensor/main/ring_buffer.c/.h` — 60s PSRAM history
- `firmware/openrecall_sensor/test/test_c6_packet.c` — golden-bytes contract test

**Android relay:**
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/ble/SensorLink.kt` — BLE
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/net/ServerSocket.kt` — WS
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/protocol/Messages.kt` — §E
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/RelaySession.kt` — relay brain
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/RelayService.kt` — foreground service
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/data/SpeakerActions.kt` — rename seam
- `android/openrecall-relay/app/src/main/kotlin/com/openrecall/relay/http/OpenRecallHttpClient.kt` — HTTP

**Server:**
- `server/scripts/run_gateway.py` — entry point
- `server/src/openrecall_server/gateway/core.py` — GatewayCore state machine
- `server/src/openrecall_server/gateway/adapter.py` — WS transport
- `server/src/openrecall_server/ingest/audio_packet.py` — §C.6 decode/encode
- `server/src/openrecall_server/ingest/reassembler.py` — reassembly + Bug A
- `server/src/openrecall_server/ingest/pipeline.py` — ingest pipeline
- `server/src/openrecall_server/ingest/speaker_identifier.py` — speaker ID
- `server/src/openrecall_server/memory/extraction_worker.py` — extraction worker
- `server/src/openrecall_server/memory/stages.py` — pipeline stages
- `server/src/openrecall_server/agent/planner.py` — Planner
- `server/src/openrecall_server/agent/proactive.py` — ProactiveTriggerEngine
- `server/src/openrecall_server/commands/signing.py` — Ed25519
- `server/src/openrecall_server/commands/dispatcher.py` — issue/ack/pending
- `server/src/openrecall_server/protocol/messages.py` — §E
- `server/src/openrecall_server/http/routes/speakers.py` — rename/reassign
- `server/src/openrecall_server/http/app.py` — build_app

**Docs:**
- `docs/bring-up/2026-07-27-real-device-bringup.md` — tiered runbook
- `README.md` — root README