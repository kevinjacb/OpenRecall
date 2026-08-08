package com.opensapien.relay.data

import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.domain.model.ServerStatus
import kotlinx.coroutines.flow.Flow

/**
 * Source of the Dashboard's server-status snapshot. The Phase 4
 * implementation ([PollingStatusRepository]) polls `GET /status`
 * on a foreground-only cadence and caches the latest [Outcome].
 */
interface StatusRepository {
    /**
     * Hot stream of the latest server status. Emits the most recent
     * poll result on subscribe (the cached value), then a fresh
     * [Outcome] on every subsequent poll. Failures are surfaced as
     * [Outcome.Failure] rather than thrown, so a subscriber never
     * has to wrap the flow in a try/catch.
     */
    fun observeStatus(): Flow<Outcome<ServerStatus>>

    /**
     * Force an immediate poll out-of-cycle (e.g. a pull-to-refresh gesture).
     * Default is a no-op so fakes that don't model polling stay compatible;
     * [PollingStatusRepository] overrides it to run one fetch right away and
     * publish the result, without waiting for the next interval tick.
     *
     * Suspending so a caller can await completion to toggle a refresh spinner.
     */
    suspend fun refresh() {}
}
