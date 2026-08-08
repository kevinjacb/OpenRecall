package com.opensapien.relay.core.model

/**
 * The set of "things that can go wrong" the UI cares about, regardless of
 * whether the source is HTTP, the relay, or the local DB. Repositories
 * normalize raw exceptions into one of these; UI maps them to user-visible
 * messages. The set is deliberately small — anything outside these four is
 * a `Unknown(throwable)` we forgot to classify.
 */
sealed interface ApiError {
    /** Auth failed (HTTP 401/403, or a server-side auth refusal). The
     *  Settings screen is the right next step. */
    data object Unauthorized : ApiError

    /** The server isn't reachable (DNS, TCP, TLS, timeout). Includes a
     *  short human-readable reason for diagnostics. */
    data class Unreachable(val reason: String) : ApiError

    /** The server replied with a non-success HTTP status that isn't auth. */
    data class Http(val code: Int) : ApiError

    /** Anything we haven't classified. Carry the throwable for logging. */
    data class Unknown(val throwable: Throwable) : ApiError
}
