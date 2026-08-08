package com.openrecall.relay.data

import com.openrecall.relay.core.model.PagedResult
import com.openrecall.relay.core.ui.toDisplayMessage
import com.openrecall.relay.data.httpApiError
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.relay.RelayController
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.catch
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

/** Number of recent recordings surfaced on the Home dashboard. */
private const val RECENT_SEGMENT_COUNT = 3

/**
 * The Home screen's single source of truth. Fans the relay controller, the
 * server-status poller, and the recordings list into one [DashboardState].
 */
interface DashboardRepository {
    /** Hot, conflated stream of the aggregated dashboard state. Starts
     *  [DashboardState.Loading] and flips to [DashboardState.Loaded] once
     *  the first status poll returns (the other two sources are hot and
     *  have a value immediately). */
    fun observe(): StateFlow<DashboardState>

    /** Pull-to-refresh: force an immediate status poll AND reset the
     *  recordings list to page 1. The [observe] stream re-emits as each child refresh
     *  publishes its new value. Default is a no-op so fakes stay compatible;
     *  [DashboardRepositoryImpl] overrides it. */
    suspend fun refresh() {}
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
    private val segmentRepo: SegmentRepository,
    private val scope: CoroutineScope,
) : DashboardRepository {

    private val flow: StateFlow<DashboardState> =
        combine(
            relayController.state,
            statusRepo.observeStatus(),
            segmentRepo.observeSegments(),
        ) { relay, status, paged ->
            val loaded: DashboardState = DashboardState.Loaded(
                relay = relay,
                server = status,
                recentSegments = paged.recentItems(),
            )
            loaded
        }
            .catch { emit(DashboardState.Failed(httpApiError(it).toDisplayMessage())) }
            .stateIn(scope, SharingStarted.WhileSubscribed(5_000), DashboardState.Loading)

    override fun observe(): StateFlow<DashboardState> = flow

    override suspend fun refresh() {
        // Order: refresh the status first (cheap, immediate), then reset the
        // recordings list to page 1. Both publish into the combined [flow], so
        // the Home screen re-emits as each child refresh lands. Relays run
        // concurrently to the relay controller; we don't touch it here (the
        // manual "Retry connection" action handles relay re-provisioning).
        statusRepo.refresh()
        segmentRepo.refresh()
    }
}

/** The first [RECENT_SEGMENT_COUNT] items of a paged list, or empty for any
 *  non-[PagedResult.Page] state (Loading / Exhausted / Error). */
private fun PagedResult<Segment>.recentItems(): List<Segment> =
    (this as? PagedResult.Page)?.items?.take(RECENT_SEGMENT_COUNT) ?: emptyList()
