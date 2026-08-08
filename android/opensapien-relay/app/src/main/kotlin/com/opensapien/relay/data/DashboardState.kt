package com.opensapien.relay.data

import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.domain.model.ServerStatus
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.relay.RelayState

/**
 * The aggregated snapshot the Home screen renders. Produced by
 * [DashboardRepository] by fanning three independent sources into one:
 * the relay/connection [RelayState], the polled server [ServerStatus]
 * (wrapped in an [Outcome] so an unreachable server is a rendered state,
 * not a crash), and the most-recent handful of [SessionSummary]s.
 *
 * Sealed so the Home ViewModel/screen can render it with one exhaustive
 * `when`.
 */
sealed interface DashboardState {
    /** No source has produced its first value yet (specifically, the
     *  first status poll hasn't returned). */
    data object Loading : DashboardState

    /** All three sources have a value. [server] carries the latest poll
     *  [Outcome] — a [Outcome.Failure] here means "server unreachable /
     *  unauthorized", which the screen renders as an offline card rather
     *  than an error page. */
    data class Loaded(
        val relay: RelayState,
        val server: Outcome<ServerStatus>,
        val recentSessions: List<SessionSummary>,
    ) : DashboardState

    /** The aggregate flow itself errored (rare — the individual sources
     *  don't throw; this is a defensive catch). */
    data class Failed(val reason: String) : DashboardState
}
