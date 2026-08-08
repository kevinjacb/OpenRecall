package com.opensapien.relay.relay

/**
 * What the relay knows about the server. The relay's view of the
 * server is independent of the BLE/relay connection (the relay can
 * know the server is up before BLE connects, or after a reconnect).
 *
 * Sealed so the UI can exhaustively render.
 */
sealed interface ServerState {
    /** Haven't checked. */
    data object Unknown : ServerState

    /** The server responded to a health probe. */
    data object Reachable : ServerState

    /** The server accepted our bearer token. */
    data object Authenticated : ServerState

    /** The server is unreachable. `reason` is a short why (DNS, TCP,
     *  TLS, timeout). */
    data class Unreachable(val reason: String) : ServerState

    /** A sync operation is in flight (e.g. backfill). Distinct from
     *  Reachable so the UI can show a "syncing…" affordance. */
    data object Syncing : ServerState
}
