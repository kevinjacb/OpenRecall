package com.opensapien.relay.relay

/**
 * Exponential backoff schedule for [RelayService]'s auto-reconnect. Pure (no
 * Android, no I/O) so the retry sequence is unit-testable without the service.
 *
 * The sequence is 1s → 2s → 4s → 8s → 30s (cap). Attempts beyond the last
 * explicit step clamp to the 30s cap. After [MAX_ATTEMPTS] attempts the caller
 * gives up — [shouldGiveUp] returns true — and publishes a terminal
 * [RelayConnectionState.Failed]; the manual "Retry connection" button (which
 * re-launches the service) is the escape hatch.
 *
 * `attempt` is 0-indexed: the first retry uses [nextBackoffMs]`(0)` = 1s.
 */
object Backoff {
    /** How many reconnect attempts before giving up (publishing Failed). */
    const val MAX_ATTEMPTS = 6

    private val STEPS = longArrayOf(1_000L, 2_000L, 4_000L, 8_000L, 30_000L)

    /** Delay to wait before the (attempt+1)-th retry. Clamps to the 30s cap. */
    fun nextBackoffMs(attempt: Int): Long {
        val i = attempt.coerceAtLeast(0)
        return if (i >= STEPS.size) STEPS.last() else STEPS[i]
    }

    /** True once [attempt] has exhausted [MAX_ATTEMPTS] — the caller should
     *  publish a terminal [RelayConnectionState.Failed] and stop retrying. */
    fun shouldGiveUp(attempt: Int): Boolean = attempt >= MAX_ATTEMPTS
}