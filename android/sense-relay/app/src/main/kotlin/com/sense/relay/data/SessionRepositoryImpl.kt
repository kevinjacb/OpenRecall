package com.sense.relay.data

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.model.PagedResult
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.http.dto.toDomain
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Sessions per page — matches the server's default `limit`. */
private const val PAGE_SIZE = 20

/**
 * Production [SessionRepository]. The paged list is an internal
 * [MutableStateFlow] so paging is replayable (a late subscriber sees the
 * accumulated pages), matching the Phase-2 [FakeSessionRepository] contract
 * exactly:
 *   - Before the first [loadMoreSessions], the flow is [PagedResult.Loading].
 *   - A non-empty page emits [PagedResult.Page] with the *accumulated* items
 *     and the next cursor; a null cursor marks the last page (data kept, the
 *     next call is a no-op).
 *   - An empty terminal page (empty list + null cursor) with nothing
 *     accumulated emits [PagedResult.Exhausted]; with accumulated pages it
 *     re-emits the accumulated [PagedResult.Page] with a null cursor (the
 *     list is complete, no data lost). This DELIBERATELY diverges from
 *     [FakeSessionRepository], which always emits Exhausted here — emitting
 *     Exhausted with accumulated data would make the UI map it to Empty and
 *     discard the loaded sessions.
 *   - A fetch failure emits [PagedResult.Error] and does NOT exhaust the
 *     list, so a later [loadMoreSessions] retries the same page (recovery).
 *
 * All network access goes through the injected [SessionApi] seam (the
 * concrete [com.sense.relay.http.SenseHttpClient] is `final` and does I/O,
 * so this is what makes the repository host-testable). Every fetch catches,
 * re-throws [CancellationException], and maps other throwables to an error
 * state — no path throws out of a suspend fn (the Phase-4 crash lesson).
 *
 * [loadMoreSessions] is guarded by a [Mutex] so concurrent calls (e.g. a
 * fast scroll firing the load-more trigger twice) don't double-fetch or
 * race the accumulator.
 */
class SessionRepositoryImpl(
    private val api: SessionApi,
    private val onUnknownKind: (String) -> Unit = {},
) : SessionRepository {

    private val pages = MutableStateFlow<PagedResult<SessionSummary>>(PagedResult.Loading)
    private val loadErrors = MutableStateFlow<ApiError?>(null)
    private val accumulated = mutableListOf<SessionSummary>()
    private var cursor: String? = null
    private var started = false
    private var exhausted = false
    private val loadMutex = Mutex()

    override fun observeSessions(): Flow<PagedResult<SessionSummary>> = pages.asStateFlow()

    override fun observeLoadErrors(): Flow<ApiError?> = loadErrors.asStateFlow()

    override suspend fun loadMoreSessions() = loadMutex.withLock {
        if (exhausted) return@withLock
        try {
            val page = api.listSessions(PAGE_SIZE, if (started) cursor else null)
            started = true
            // Any successful fetch clears a prior inline error — placed
            // here (before the empty-terminal early return) so the empty-
            // terminal success branch also clears it. Otherwise a retry
            // that lands on an empty terminal page would leave the error
            // stuck AND set `exhausted`, making Retry a no-op (unrecoverable).
            loadErrors.value = null
            val items = page.sessions.map { it.toDomain() }
            if (items.isEmpty() && page.nextCursor == null) {
                // The server's "nothing to return, end of list" signal.
                // Divergence from FakeSessionRepository (which always emits
                // Exhausted here): when we already have accumulated pages,
                // emitting Exhausted would make the UI map it to Empty and
                // DISCARD the loaded sessions. Instead, re-emit the
                // accumulated Page with a null cursor so the UI sees
                // canLoadMore=false (the list is complete, no data lost).
                // The cursor is cleared so `exhausted` and the emitted
                // cursor stay consistent.
                if (accumulated.isEmpty()) {
                    pages.value = PagedResult.Exhausted
                } else {
                    cursor = null
                    pages.value = PagedResult.Page(accumulated.toList(), null)
                }
                exhausted = true
                return@withLock
            }
            accumulated.addAll(items)
            cursor = page.nextCursor
            pages.value = PagedResult.Page(accumulated.toList(), cursor)
            // A null cursor means "this was the last page": keep the data,
            // future calls are no-ops.
            if (cursor == null) exhausted = true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            // Surface the failure without exhausting: a later loadMore can
            // retry the same page (the cursor/started state is unchanged on
            // the failure path, since we only advance them after a success).
            // If we already have items, KEEP the Page (don't discard the list
            // for a paging failure) and surface the error as a side-channel
            // via `loadErrors` so the UI shows an inline "Retry" row. If the
            // initial load failed (no items), emit a full-screen `Error`.
            if (accumulated.isNotEmpty()) {
                loadErrors.value = httpApiError(e)
                // Re-assert the current Page so a prior Error (from the very
                // first failed attempt) is replaced by the recovered list.
                pages.value = PagedResult.Page(accumulated.toList(), cursor)
            } else {
                pages.value = PagedResult.Error(e)
            }
        }
    }

    override fun observeSession(id: SessionId): Flow<Outcome<SessionDetails>> = flow {
        emit(fetchSession(id))
    }

    override fun observeSessionEvents(id: SessionId): Flow<Outcome<List<CaptureEvent>>> = flow {
        emit(fetchEvents(id))
    }

    private suspend fun fetchSession(id: SessionId): Outcome<SessionDetails> =
        try {
            val dto = api.getSession(id)
            Outcome.Success(
                SessionDetails(
                    summary = dto.summary.toDomain(),
                    // The shared SessionDetailsDto.toDomain() drops events; map
                    // them here (sorted by seq) so the detail payload is whole.
                    events = dto.events.toDomain(onUnknownKind).sortedBy { it.seq },
                ),
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            Outcome.Failure(httpApiError(e))
        }

    private suspend fun fetchEvents(id: SessionId): Outcome<List<CaptureEvent>> =
        try {
            Outcome.Success(api.getSessionEvents(id).toDomain(onUnknownKind).sortedBy { it.seq })
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            Outcome.Failure(httpApiError(e))
        }
}
