package com.openrecall.relay.relay

/**
 * The composite state the relay publishes. Read by every UI that
 * needs to know what's going on (Home, Device, Recordings, …). The
 * only writer is [RelayController]; [RelayService] is the only
 * caller of the controller's `updateX` methods.
 *
 * `lastError` is a "soft" error slot — the most recent non-fatal
 * message (BLE stack reason, server 4xx, etc.). Distinct from the
 * hard failure encoded in [RelayConnectionState.Failed] /
 * [ServerState.Unreachable], which is the headline state.
 *
 * `revision` is a monotonically increasing counter that bumps on
 * every [RelayController.requestRefresh] call. It exists so that
 * `data class`-equal re-emits (e.g. the controller's content is
 * unchanged but a re-evaluation is requested) still cross the
 * `MutableStateFlow.value` equality check. Consumers that want
 * to react to a refresh should key on `revision`; consumers that
 * only care about the headline state should ignore it.
 *
 * Mutability: this is a `data class` because consumers destructure
 * the fields. Field updates happen in one place (the
 * controller) by `copy`-ing the current state. There is no
 * `MutableRelayState` type — that would invite parallel writers,
 * which is exactly what the singleton is preventing.
 */
data class RelayState(
    val device: DeviceState,
    val connection: RelayConnectionState,
    val server: ServerState,
    val lastError: String?,
    val revision: Int = 0,
) {
    companion object {
        /** The starting point — everything unknown, no error. */
        val Initial = RelayState(
            device = DeviceState.Unknown,
            connection = RelayConnectionState.Idle,
            server = ServerState.Unknown,
            lastError = null,
        )
    }
}
