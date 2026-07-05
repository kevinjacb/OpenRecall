package com.sense.relay.data

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.ServerStatus
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flowOf

/**
 * Source of the Dashboard's server-status snapshot. The real
 * implementation (Phase 4) polls `GET /status`; Phase 2 ships
 * the interface + a no-op placeholder so `RepositoryModule` and
 * later phases compile.
 */
interface StatusRepository {
    /**
     * Hot stream of the latest server status. The Phase 4
     * implementation polls; the Phase 2 no-op emits a single
     * [Outcome.Failure] on subscribe and goes quiet.
     */
    fun observeStatus(): Flow<Outcome<ServerStatus>>
}

/**
 * Phase 2 placeholder. Emits a single `Unreachable` failure on
 * subscribe — there is no server to ask, and the wiring needs a
 * concrete type. Phase 4's [PollingStatusRepository] replaces
 * this; consumers should not depend on the placeholder's specifics.
 */
class NoopStatusRepository : StatusRepository {
    override fun observeStatus(): Flow<Outcome<ServerStatus>> =
        flowOf(Outcome.Failure(ApiError.Unreachable("not yet wired (Phase 4)")))
}
