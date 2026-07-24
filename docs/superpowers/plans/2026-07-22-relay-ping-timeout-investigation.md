# Investigation: relay fails with "ping didn't receive" after a while

**Date:** 2026-07-22
**Status:** Root cause analysis complete. No fix yet.
**Symptom:** Relay (Android) shows `RelayConnectionState.Failed` with reason
containing "ping didn't receive". Tries to reconnect — server is unreachable.
After a while of operation (not on first connect), this happens repeatedly.

## Phase 1 — Root cause: the Android WebSocket reader thread blocks

The Android relay's WebSocket (`ServerSocket.kt`) sets:

```kotlin
.pingInterval(20, TimeUnit.SECONDS)   // keepalive; also detects dead links
```

OkHttp's `RealWebSocket` uses this single parameter as **both** the ping send
period **and** the implicit pong timeout. From `RealWebSocket.kt`:

```kotlin
internal fun writePingFrame() {
    ...
    synchronized(this) {
      failedPing = if (awaitingPong) sentPingCount else -1
      sentPingCount++
      awaitingPong = true
    }
    if (failedPing != -1) {
      failWebSocket(SocketTimeoutException(
        "sent ping but didn't receive pong within ${pingIntervalMillis}ms "
        + "(after ${failedPing - 1} successful ping/pongs)"), null)
      return
    }
    writer.writePing(ByteString.EMPTY)
}
```

So the failure path is: client sends ping #N at t=0 (with `awaitingPong=true`),
at t=20 the TaskRunner tries to send ping #N+1, sees `awaitingPong` still true,
fires the timeout with the user's exact error string.

**The pong is processed by the Android reader thread, not the writer or
ping-scheduler thread.** The OkHttp reader thread is a single dedicated thread
that calls `reader.processNextFrame()` in a loop; each frame is dispatched to
listener callbacks synchronously. If the reader thread is blocked on any
synchronous I/O for >20s, the pong never gets processed and the timeout fires.

The server is not the problem. Verified empirically:

```
[server] doing 5s of heavy synchronous work in a worker thread
[client] pings every 1s; every ping receives a pong in 0ms
[server] heavy work done; subsequent pings also 0ms
```

The websockets library auto-pongs in `data_received` (a transport callback
that runs whenever the asyncio event loop is available), so pongs are
promptly sent even while the server is processing. The server's
`ping_interval=20, ping_timeout=20` defaults are symmetric and healthy.

## What runs on the Android reader thread

`RelayService.socketListener.onText` runs on the WebSocket reader thread:

```kotlin
override fun onText(text: String) {
    if (stale()) return
    session.onServerMessage(text).forEach(::execute)
}
```

`execute(action)` is also on the reader thread:

```kotlin
private fun execute(action: RelayAction) {
    when (action) {
        is RelayAction.SendServerBinary -> socket?.sendBinary(action.data)
        is RelayAction.SendServerText -> socket?.sendText(action.text)
        is RelayAction.WriteDeviceCommand -> sensor.writeCommand(action.frame)
        is RelayAction.ForwardToChatHistory ->
            RepositoryModule.repos.chatHistoryStore.append(action.message)
        is RelayAction.Note -> Log.i(TAG, action.message)
    }
}
```

The blocking candidates:

### 1. `sensor.writeCommand(action.frame)` — BLE GATT write (most likely)

`SensorLink.writeCommand` calls `enqueue { ... gatt?.writeCharacteristic(ch) }`.
`enqueue` adds the op to an in-memory queue and `drain()` runs `op()`
**synchronously on the caller thread** — which is the WebSocket reader
thread (see `SensorLink.kt:166-181`). `BluetoothGattCharacteristic.writeCharacteristic`
acquires internal locks and submits the write to the GATT queue. Under normal
operation this is fast (a few ms), but it **blocks the caller until the
previous write completes**. If the device is unresponsive (sleeping,
reconnecting, link in a bad state) the previous GATT callback doesn't fire,
and `writeCharacteristic` blocks indefinitely. The reader thread is then
stuck, pongs don't get processed, the 20s OkHttp timer fires.

This matches "after a while": BLE state degrades over time on flaky links.

### 2. `chatHistoryStore.append(action.message)` — list concatenation

`ChatHistoryStore.append`:
```kotlin
fun append(msg: ChatMessage) {
    _messages.update { it + msg }   // O(n) list concat
    unflushedDelta += 1
}
```

`StateFlow.update` invokes the block under a CAS loop. Each `it + msg`
allocates a new list of size n+1 and copies n references. With a long-running
session and many proactive messages, this becomes slow. Still, "slow" here
means microseconds-to-milliseconds per call; only the cumulative impact of
many `update { it + msg }` calls during a single synchronous burst could
matter, and it would need tens of thousands of messages to push past 20s.

This is a contributing factor but unlikely to be the proximate cause.

### 3. `Log.i(TAG, action.message)` — logging

`Log.i` is fast and non-blocking. Not a contributor.

## Why "after a while" specifically

- The OkHttp ping timer starts on connect. The first 20s are usually clean
  because no commands have been sent yet.
- Once the server starts sending `command` frames (P2-commands: `record_video`,
  `capture_photo`, etc.), the Android reader thread starts running
  `writeCharacteristic` for each.
- If the BLE link is flaky, occasional `writeCharacteristic` calls start to
  block (waiting for a GATT callback that doesn't come). The reader thread
  stalls; pongs queue up; the next 20s timer fires.
- Once the WebSocket is closed by the client, the relay enters `Failed`
  state. The Android side **only opens a new socket on BLE reconnect**
  (see `RelayService.sensorListener.onConnected`, line 130:
  `socket = ServerSocket(...).also { it.connect() }`). If the BLE link
  is still flaky, the relay keeps failing.

## Why "server goes offline"

The server process is not actually down. The user's "server is offline"
perception is the relay's `Failed` state. The actual chain:

1. OkHttp reader thread blocks > 20s on `writeCharacteristic`.
2. OkHttp's `writePingFrame` fires the timeout, calls `failWebSocket`.
3. `WebSocketListener.onFailure` → `ServerSocket.onFailure` →
   `listener.onClosed(t.message)`.
4. `RelayService.socketListener.onClosed` →
   `RelayController.updateConnection(Failed(reason))` →
   UI shows "Failed: sent ping but didn't receive pong within 20000ms".
5. The relay does NOT attempt to reconnect — it waits for the next BLE
   reconnect to open a new socket. From the user's POV, the server is
   "offline" because the relay shows failed.

The server's own keepalive would have fired ~20s later (after the client's
timeout) with `keepalive ping timeout` and closed the connection, but the
client side wins the race.

## Phase 2 — Working examples in the codebase

None. The WebSocket keepalive / reader-thread contract is not exercised by
tests. The closest analog is the server-side proactive drain task, which
explicitly offloads blocking I/O:

```python
# gateway/adapter.py:226-244
async for message in ws:
    try:
        replies = await asyncio.to_thread(handle_message, core, message)
```

The server side has internalized "blocking I/O must not run on the event
loop / reader thread." The Android side has not.

## Phase 3 — Hypothesis

**The Android WebSocket reader thread is running synchronous BLE GATT
writes (`writeCharacteristic`) on every inbound `command` frame. When the
BLE link degrades, those writes block. The reader thread is then stuck for
>20s, the next ping timer fires, the WebSocket is closed, the relay
transitions to `Failed`. The server is not at fault.**

## Phase 4 — Open questions / what to fix (not yet)

1. **Offload `execute(action)` to a background dispatcher** so no action's
   I/O runs on the reader thread. `Dispatchers.IO` or a dedicated
   `newSingleThreadExecutor` is sufficient. This is the structural fix.

2. **Add a watchdog / pong-failure observability seam** so future
   occurrences of this class of bug are visible (e.g. a `ping_lag_ms`
   histogram on the Android side).

3. **Increase the OkHttp `pingInterval` to >BLE-worst-case** as a defense
   in depth. 20s is the OkHttp default and is appropriate for a healthy
   link, but the BLE write path is unbounded, so the only robust answer
   is to keep the reader thread free of blocking I/O — not to widen the
   ping window.

4. **Add a test that reproduces the failure**: a fake `SensorLink` whose
   `writeCommand` blocks for >20s, a stub `ServerSocket` driver, and an
   assertion that the WebSocket is still alive (or at minimum that the
   pong watchdog fires) after the BLE stall. This is the regression guard.

## What was ruled out

- **Server's event loop blocked by transcription**: ruled out by the
  5s-blocking worker-thread repro above. Pongs get through in 0ms.
- **asyncio.Queue cross-thread race** (the M4.3 issue): unrelated. The
  enqueuer's `bind_loop` + `call_soon_threadsafe` fix is in the queue
  path, not the WebSocket reader.
- **Default executor starvation**: default pool size is min(32, cpu+4).
  On the dev machine that's 22 threads; on a small device maybe 8.
  None of the to_thread callers hold their thread for >5s in normal
  operation, so the pool is healthy. Even at full saturation, the
  `to_thread` await releases the loop, so pongs still get through.
- **Bounded `recv` queue on the websockets server**: `max_queue=16`
  by default. If the server fell behind, `recv` would raise `QueueFull`,
  not silently drop pings. Pings are processed at the protocol layer
  (in `data_received`), not via `recv`, so this is irrelevant to the
  ping path.

## What needs user input before fixing

- Should the fix be Android-only (offload `execute`) or also include
  the Android test that reproduces the BLE-stall scenario?
- Is the BLE write path on the relay actually being used in production
  (P2-commands), or is this a theoretical concern until the firmware
  executors land?
- Is the user's "server is offline" perception a server-process issue
  I'm missing, or purely the relay's `Failed` state? Worth confirming
  with one more diagnostic (server log around the time of failure) before
  committing to the Android-side fix.
