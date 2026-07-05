package com.sense.relay.data

import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.ServerStatus
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
}
