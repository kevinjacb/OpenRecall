# OpenRecall Relay (Android)

The middle tier: a dumb, reliable **pipe with session bookkeeping** that bridges the
wearable's BLE link to the server's WebSocket. It is the only tier that speaks **§E**
(session framing); the device speaks §C.6 audio + §D commands, the server owns memory.

```
 WEARABLE (BLE)                 RELAY (this app)                 SERVER (WS)
 ──────────────                 ────────────────                 ───────────
 §C.6 audio  ──notify──▶  forward verbatim (binary)  ──────────▶  ingest
 §D command  ◀─write───   sig‖payload  ◀── decode ──── command ──  signed §D
 ack (id)    ──notify──▶  wrap as §E command_ack  ────────────▶   dispatcher
                          + owns hello / bye / cursor
```

## Architecture

The protocol brain is **pure and unit-tested**, separated from Android I/O — the same
discipline as the server's `GatewayCore`:

| File | Role | Verified? |
|---|---|---|
| `RelaySession.kt` | the bridge logic; consumes events, returns `RelayAction`s | ✅ unit tests |
| `protocol/Messages.kt` | §E encode/parse (lenient on unknown types) | ✅ unit tests |
| `ble/SensorLink.kt` | BLE central: scan/connect/subscribe/write (serial GATT queue) | ⚠️ needs on-device |
| `net/ServerSocket.kt` | OkHttp WebSocket transport | ⚠️ needs on-device |
| `RelayService.kt` | foreground service wiring it together | ⚠️ needs on-device |

`RelaySession` never does I/O: it returns `SendServerBinary` / `SendServerText` /
`WriteDeviceCommand` / `Note`, which `RelayService` executes. That keeps the
correctness-critical translation (including turning a server `command`'s base64 sig +
JSON payload into the device's `[raw 64-byte sig][payload]` frame) testable on the JVM.

## Build & run

Open in **Android Studio** (or `./gradlew`), then:

```bash
./gradlew test                 # runs RelaySessionTest — the executable spec
./gradlew :app:assembleDebug   # builds the APK
```

Start the relay pointing at your gateway (host IP; `10.0.2.2` is host-loopback from the
emulator):

```kotlin
startForegroundService(Intent(ctx, RelayService::class.java)
    .putExtra("server_url", "ws://192.168.1.20:8765"))
```

Grant `BLUETOOTH_SCAN` / `BLUETOOTH_CONNECT` at runtime (Android 12+).

## Status & honest gaps

- **Verified here:** the protocol brain (`RelaySession` + `Messages`) via JVM unit
  tests — the part that must match the server (`§E`) and the device (`§C.6`/`§D`).
- **Not yet validated on a phone:** the BLE GATT layer. Android GATT is finicky —
  operations must be serialised (done via `opQueue`), and subscribe order / MTU
  negotiation / notification delivery vary by handset. Verify on real hardware.
- **Not yet handled:** server `request_chunks` backfill (needs the device's §C.6
  HISTORICAL / `request_buffer` path) — currently logged. Audio is best-effort;
  durability comes from the device ring buffer + resync, per the design.

## Where this fits

With this tier, all three exist: **wearable firmware** (`firmware/openrecall_sensor`, ESP-IDF)
→ **relay** (this) → **server** (`server/`, 132 tests). The relay's `RelaySession` is the
Android counterpart to the server's reference `DeviceClient` — together they specify the
whole wire contract.
