package com.sense.relay.data

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.model.PagedResult
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flowOf

/**
 * Source of truth for the recordings list and the per-session detail
 * timeline. ViewModels subscribe; this is the only thing the rest of
 * the app depends on. The real implementation (Phase 5) calls
 * [com.sense.relay.http.SenseHttpClient]; Phase 2 ships a
 * [FakeSessionRepository] that serves pre-queued pages and emits
 * `Failure` for the per-session flows until Phase 5 wires them.
 *
 * The paged-list flow is a [StateFlow]; consumers read its current
 * value. Emission history is not preserved — by design, since a
 * StateFlow models "what is the latest state," not "what series of
 * states did we pass through."
 */
interface SessionRepository {
    /** Current paged list. Emits [PagedResult.Loading] before any
     *  [loadMoreSessions] call; [PagedResult.Page] (with all
     *  accumulated items) after each load; [PagedResult.Exhausted]
     *  when the server signals end-of-list. */
    fun observeSessions(): Flow<PagedResult<SessionSummary>>

    /** Trigger a fetch of the next page. The flow from
     *  [observeSessions] reflects the new state. Idempotent: a
     *  call after the list is [PagedResult.Exhausted] is a no-op. */
    suspend fun loadMoreSessions()

    /** Per-session detail (summary + events). Emits once on
     *  subscribe; Phase 5 backs this with `GET /sessions/{id}`. */
    fun observeSession(id: SessionId): Flow<Outcome<SessionDetails>>

    /** Per-session event timeline. Emits once on subscribe; Phase 5
     *  backs this with `GET /sessions/{id}/events`. */
    fun observeSessionEvents(id: SessionId): Flow<Outcome<List<CaptureEvent>>>

    /**
     * Transient paging errors that should NOT replace the list — when a
     * `loadMoreSessions` fails AFTER items have loaded, the accumulated
     * `Page` is kept and the error is surfaced here as a side-channel so
     * the UI can show a small inline "Retry" row instead of a full-screen
     * error. Emits `null` when there's no current error (e.g. after a
     * successful retry clears it). The default emits a single `null` (no
     * error) so `combine`-based consumers fire once on subscribe; the
     * [FakeSessionRepository] and other test fakes inherit it unchanged.
     */
    fun observeLoadErrors(): Flow<ApiError?> = flowOf(null)
}

/**
 * In-memory [SessionRepository] used as a test fixture in Phase 4/5
 * and as the production wiring in Phase 2 (the real impl is Phase 5).
 *
 * **Paging semantics** (matches the brief's [PagedResult] contract):
 *   - Before any [loadMoreSessions], the flow is [PagedResult.Loading].
 *   - After loading a non-empty page, the flow is
 *     [PagedResult.Page] with the *accumulated* items and the new
 *     cursor. A null cursor is a signal that the next loadMore will
 *     either be a no-op (we already have the data) or flip to
 *     [PagedResult.Exhausted].
 *   - After loading an *empty* page with a null cursor (the server's
 *     "nothing to return" signal), the flow is [PagedResult.Exhausted].
 *   - The per-session flows emit a stable [Outcome.Failure] until
 *     Phase 5 — no server, no detail data.
 */
class FakeSessionRepository : SessionRepository {

    /** A queued page: items + the cursor for the next fetch (null = end). */
    data class QueuedPage(
        val items: List<SessionSummary>,
        val nextCursor: String?,
    )

    private val queued = ArrayDeque<QueuedPage>()
    private val accumulated = mutableListOf<SessionSummary>()
    private var exhausted = false

    private val pages = MutableStateFlow<PagedResult<SessionSummary>>(PagedResult.Loading)

    /** Test-only: enqueue a page to be served on the next loadMore. */
    fun queue(items: List<SessionSummary>, nextCursor: String?) {
        queued.addLast(QueuedPage(items, nextCursor))
    }

    override fun observeSessions(): Flow<PagedResult<SessionSummary>> = pages.asStateFlow()

    override suspend fun loadMoreSessions() {
        if (exhausted) return
        if (queued.isEmpty()) {
            // No more queued pages. If we have data, the previous
            // Page's null cursor already told consumers the list is
            // done — go quiet. If we have no data at all, the list
            // is empty/exhausted.
            if (accumulated.isEmpty()) {
                pages.value = PagedResult.Exhausted
            }
            exhausted = true
            return
        }
        val (items, cursor) = queued.removeFirst()
        // The "empty terminal page" case is the server's
        // "nothing to return, end of list" signal — emit Exhausted
        // so the UI stops calling loadMore. (This is distinct from
        // a non-empty page with a null cursor, where we keep the
        // data and the next call flips to Exhausted.)
        if (items.isEmpty() && cursor == null) {
            pages.value = PagedResult.Exhausted
            exhausted = true
            return
        }
        accumulated.addAll(items)
        pages.value = PagedResult.Page(
            items = accumulated.toList(),
            nextCursor = cursor,
        )
        if (cursor == null) {
            // Server said "this is the last page." Future calls are
            // no-ops; the data is still on the wire for the UI.
            exhausted = true
        }
    }

    override fun observeSession(id: SessionId): Flow<Outcome<SessionDetails>> =
        flowOf(Outcome.Failure(ApiError.Unreachable("not yet wired (Phase 5)")))

    override fun observeSessionEvents(id: SessionId): Flow<Outcome<List<CaptureEvent>>> =
        flowOf(Outcome.Failure(ApiError.Unreachable("not yet wired (Phase 5)")))
}
