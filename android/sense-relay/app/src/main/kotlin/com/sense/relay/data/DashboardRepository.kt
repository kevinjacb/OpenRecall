package com.sense.relay.data

import com.sense.relay.core.model.PagedResult
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.relay.RelayController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

/** Number of recent sessions surfaced on the Home dashboard. */
private const val RECENT_SESSION_COUNT = 3

/**
 * The Home screen's single source of truth. Fans the relay controller, the
 * server-status poller, and the session list into one [DashboardState].
 */
interface DashboardRepository {
    /** Hot, conflated stream of the aggregated dashboard state. Starts
     *  [DashboardState.Loading] and flips to [DashboardState.Loaded] once
     *  the first status poll returns (the other two sources are hot and
     *  have a value immediately). */
    fun observe(): StateFlow<DashboardState>
}

/**
 * Production [DashboardRepository]. `combine` re-emits whenever any of the
 * three sources changes, so the Home screen stays live. `stateIn` with
 * [SharingStarted.WhileSubscribed] keeps the upstream (crucially, the
 * status poller's subscription) alive only while the screen is on-screen,
 * with a 5s grace window across config changes.
 *
 * The [scope] is process-scoped in production (constructed once in
 * [RepositoryModule]); tests pass a controllable scope.
 */
class DashboardRepositoryImpl(
    private val relayController: RelayController,
    private val statusRepo: StatusRepository,
    private val sessionRepo: SessionRepository,
    private val scope: CoroutineScope,
) : DashboardRepository {

    private val flow: StateFlow<DashboardState> =
        combine(
            relayController.state,
            statusRepo.observeStatus(),
            sessionRepo.observeSessions(),
        ) { relay, status, paged ->
            val loaded: DashboardState = DashboardState.Loaded(
                relay = relay,
                server = status,
                recentSessions = paged.recentItems(),
            )
            loaded
        }
            .catch { emit(DashboardState.Failed(it.message ?: "dashboard error")) }
            .stateIn(scope, SharingStarted.WhileSubscribed(5_000), DashboardState.Loading)

    override fun observe(): StateFlow<DashboardState> = flow
}

/** The first [RECENT_SESSION_COUNT] items of a paged list, or empty for any
 *  non-[PagedResult.Page] state (Loading / Exhausted / Error). */
private fun PagedResult<SessionSummary>.recentItems(): List<SessionSummary> =
    (this as? PagedResult.Page)?.items?.take(RECENT_SESSION_COUNT) ?: emptyList()
