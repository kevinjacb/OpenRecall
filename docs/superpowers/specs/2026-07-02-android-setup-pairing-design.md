# Sense — Android Setup & Pairing (first interface-app slice)

**Date:** 2026-07-02
**Status:** Design, pending user review
**Scope:** First slice of turning the Android app from a headless relay into the full
user-facing interface. This slice delivers **device setup & pairing** end-to-end
across all three tiers. Conversation review, memory retrieval, Q&A, and WiFi/video
retrieval are explicitly *later slices*.

## Context & decisions (locked in brainstorm)

- **Deployment:** self-hosted AI Server reached *remotely* (not just LAN). Single
  user. ⇒ requires TLS + auth, but no multi-tenancy.
- **Auth:** shared long-lived **bearer token**, generated server-side on first run,
  copied/scanned into the phone once. Sent as `Authorization: Bearer` on every HTTP
  and WS call. TLS keeps it private in transit. (Command integrity already covered
  by existing §D Ed25519 signing — separate concern, unchanged.)
- **Device model:** single Sense wearable bound to the phone. No device list.
- **Approach:** A — new HTTP control API on the server + trust-on-first-use (TOFU)
  BLE provisioning, no BLE bonding.
- **Setup scope (minimal):** runtime provisioning of the server Ed25519 public key
  onto the device + a boot state machine + a factory reset. **Deferred:** WiFi creds
  (only needed for EOD video retrieval, a later feature) and a per-device keypair
  (only needed if the server must authenticate the device — no threat model yet).
- **UX bar:** modern, simple, **monochrome** (at most one accent for state), generous
  spacing, large touch targets, **super-fluid** animations; flows are **super
  efficient** — minimal steps, no friction.

## Architecture

The server gains a **second surface**: a small HTTP control API (aiohttp, behind the
bearer token + TLS) for request/response control & future query/Q&A work. The
existing **WebSocket stays purely the live-audio plane**. The app's setup wizard uses
HTTP for control; the relay service keeps its WS for audio. Both authenticate with
the same bearer token, and the WS — which is open today — starts requiring that token
on connect (necessary for the remote deployment).

### Server
- New `http/` package: aiohttp app + bearer-token middleware + `provisioning` route.
- `GET /health` — connectivity/ready check for the wizard.
- `GET /provisioning/pubkey` → `{pubkey: <hex>, key_id, created_at}` (reads existing
  `data/server_ed25519.key`); 503 if no key configured.
- Existing gateway WS: check bearer token on handshake, 401 otherwise.
- `run_gateway` generates/loads `data/server_token` (32 random bytes, hex) on first
  run; starts the HTTP app on a configurable port; prints URL + token + pubkey for the
  operator to transfer to the phone.
- `DeviceClient` reference sim sends the bearer token on the WS; existing e2e updated.

### Firmware
- New `provisioning` module + a new BLE **provisioning GATT service** (UUID
  `6e9d0010-…`, separate from the audio service `6e9d0001-…`) with three chars:
  - `STATE` (read + notify, 1 byte): `0=UNPROVISIONED`, `1=PROVISIONED`.
  - `SERVER_KEY` (write 32 bytes): accepted only when `UNPROVISIONED`; dropped when
    `PROVISIONED`.
  - `FACTORY_RESET` (write a magic value): wipes `sense_prov` NVS → `UNPROVISIONED`.
- The server key moves from the compile-time constant (`config.h`, kept as a `{0}`
  fallback) to **NVS** (`sense_prov` namespace: 32-byte key + `provisioned` u8).
- A **boot state machine** reads NVS and seeds the existing `commands` verifier at
  runtime via a new `commands_set_pubkey()` setter — no reboot needed after
  provisioning.
- **Audio capture/relay runs in both states.** Provisioning only gates command
  verification (which already fails today), so an unprovisioned device still streams
  audio; provisioning is what makes §D commands actuate. Existing AUDIO/COMMAND/ACK
  characteristics are untouched.

### App
- The relay is **no longer headless**: it gains its first launcher Activity — a
  Compose `SetupActivity` running a short wizard, then a minimal status screen.
- `RelayService` is extended to send the bearer token on the WS handshake and use
  `wss://`.
- Persisted config (server URL, token, device BLE address, provisioned flag) in
  DataStore.

## Components (file-level)

**Server** (`server/src/sense_server/`)
- `http/app.py` — aiohttp app factory; mounts routes + auth middleware.
- `http/auth.py` — bearer-token middleware; constant-time compare; 401 on miss.
- `http/routes/provisioning.py` — `GET /health`, `GET /provisioning/pubkey`.
- `gateway/` WS accept — bearer-token check on handshake.
- `run_gateway.py` — token gen/load + HTTP app start + operator printout.
- `DeviceClient` — send `Authorization: Bearer <token>` on WS.

**Firmware** (`firmware/sense_sensor/main/`)
- `provisioning.c/.h` (new) — NVS store, state machine, factory reset, GATT callbacks.
- `commands.c/.h` — `commands_set_pubkey(const uint8_t key[32])`; `s_pubkey` mutable;
  `commands_init` seeds from NVS at boot.
- `ble_link.c` — register provisioning service + three characteristics.
- `config.h` — provisioning service/char UUIDs, NVS namespace, FACTORY_RESET magic;
  `SERVER_ED25519_PUBKEY` kept as `{0}` fallback.
- `sense_sensor.c` — `provisioning_init()` on boot.
- `test/test_provisioning.c` (new host test) — state machine, in existing Makefile harness.

**App** (`android/sense-relay/app/src/main/kotlin/com/sense/relay/`)
- `ui/SetupActivity.kt` + `ui/SetupScreen.kt` — Compose launcher Activity, wizard UI,
  minimal post-setup status screen.
- `setup/SetupViewModel.kt` — wizard state machine (sealed state, one transition at a
  time); BLE/HTTP behind interfaces ⇒ JVM-testable.
- `setup/ProvisioningClient.kt` — BLE ops: read STATE, write SERVER_KEY, write
  FACTORY_RESET (byte framing unit-tested).
- `http/SenseHttpClient.kt` — OkHttp, bearer header, TLS with pinned CA.
- `store/ServerConfig.kt` — DataStore persistence (URL, token, device address, provisioned).
- `ble/SensorLink.kt` — extended to expose provisioning-char reads/writes; reuses scan/connect.
- `net/ServerSocket.kt` + `RelayService.kt` — bearer token on WS handshake, `wss://`.
- `AndroidManifest.xml` — add launcher Activity; add `ACCESS_NETWORK_STATE`.

## Data flow

**First-time setup:**
1. Operator starts server → `run_gateway` loads/generates token + Ed25519 key; prints
   URL + token + pubkey.
2. User opens app (now has a launcher) → wizard screen 1: enter URL + token.
3. App `GET /health`, then `GET /provisioning/pubkey` (bearer header) → server returns
   pubkey; connect test passes.
4. Wizard screen 2: BLE-scan for the Sense provisioning service → connect → read
   `STATE`.
5. If `UNPROVISIONED`: app writes the 32-byte pubkey to `SERVER_KEY`. Firmware
   validates (state==UNPROVISIONED, len==32) → **write NVS first; on NVS success →
   `commands_set_pubkey()` + set `PROVISIONED` + notify STATE**. App reads the notify →
   confirms `PROVISIONED`.
6. App persists config + device address → starts `RelayService` (wss + bearer token).
   Device now accepts signed commands.
7. Re-provision: wizard "factory-reset device" → write `FACTORY_RESET` magic → state
   back to `UNPROVISIONED` → repeat from step 5.

**Relay (unchanged except auth):** device audio (BLE §C.6) → `SensorLink` →
`RelaySession` → `ServerSocket` (wss, bearer) → server WS (now token-checked) →
existing pipeline. Commands flow back the same way, now verifiable because the device
holds the real pubkey.

## Error handling

- **Server:** 401 on bad/missing bearer token (HTTP + WS handshake); 503 if the
  gateway/pipeline isn't ready / no key configured; JSON error envelope
  `{error, detail}`.
- **Provisioning ordering (firmware):** write NVS **first**; only on NVS success →
  update RAM + state + notify. A crash mid-write leaves the device `UNPROVISIONED`;
  the app retries — the operation is idempotent (re-reading STATE and re-writing the
  key while still `UNPROVISIONED` is always safe).
- **BLE:** scan timeout → "scan again"; connection drop during provisioning → resume
  from the STATE read (idempotent); write rejected because already `PROVISIONED` → app
  offers "factory reset to re-provision."
- **Relay WS auth failure (401):** surface in status UI; offer re-setup.
- **TLS:** no "trust all" toggle. App pins a CA (operator-provided, or Let's
  Encrypt trusted by default). Self-signed-self-hosted is an operator setup step —
  documented as a known gap.

## UX

Compose, **monochrome** (black/white + at most one accent for state, e.g. a single
green/red dot for relay health), generous spacing, large touch targets, smooth
`AnimatedContent` step transitions. The wizard is short and linear — **3 screens**:
1. **Server** — URL + token (paste or QR), with a live connect test.
2. **Device** — scan → connect → provision, one screen with live status.
3. **Done** — relay running + status.

Each screen has a single primary action; progress visible but not noisy. Post-setup:
minimal status screen (relay state, last event time, factory-reset/re-setup in a
menu). BLE work off the main thread (`viewModelScope`/coroutines) to keep the
provisioning step jank-free.

## Testing

- **Server (TDD):** unit tests for bearer middleware + provisioning route; update
  existing e2e (`DeviceClient`) for the now-required WS token.
- **Firmware:** host test `test/test_provisioning.c` for the state machine + NVS
  ordering (dependency-free, like existing host tests); compile/link verification
  against the real ESP-IDF toolchain. On-device validation of the new GATT
  characteristics is a **bench-side gap** (no hardware in this env).
- **App:** JVM unit tests for `SetupViewModel` transitions and `ProvisioningClient`
  byte framing (mirrors `RelaySessionTest` as the executable spec). The existing
  `RelayServiceStartTest` instrumentation smoke stays; full on-device BLE provisioning
  can't run on an emulator (no BLE radio) — **bench-side gap**. Compose UI previewed
  in Android Studio.

## Known gaps (called out, not solved here)

- **TLS certs for a self-hosted remote server** — operator must provide a cert (Let's
  Encrypt or a self-signed CA the app pins). Documented as a setup step; no "trust all."
- **On-device BLE validation** (firmware new characteristics + app provisioning) —
  requires real hardware; deferred to bench.
- **WiFi provisioning & EOD video retrieval** — deferred (later slice).
- **Device-to-server authentication (device keypair)** — deferred (no threat model yet).

## Out of scope for this slice

Conversation review, memory retrieval/search, Q&A, any UI beyond the setup wizard +
minimal status screen, device list/multi-device, WiFi provisioning, device keypair.