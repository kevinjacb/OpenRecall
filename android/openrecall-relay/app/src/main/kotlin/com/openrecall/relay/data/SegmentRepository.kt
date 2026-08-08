package com.openrecall.relay.data

import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.model.PagedResult
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentDetails
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.Waveform
import com.openrecall.relay.http.dto.toDomain
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/** Segments per page — matches the server's default `limit`. */
private const val PAGE_SIZE = 20

/**
 * Source of truth for the recordings list and the recording-detail screen.
 *
 * Replaces the session-based list: a session is the foreground service's
 * lifetime (hours to days, spanning every reconnect), which is not what a
 * user means by "a recording". `/sessions` remains wired for diagnostics but
 * nothing user-facing reads it.
 */
interface SegmentRepository {
    /** The paged list. [PagedResult.Loading] before the first [loadMore]. */
    fun observeSegments(): Flow<PagedResult<Segment>>

    /**
     * Transient paging errors that must NOT replace the list: when a page
     * fetch fails *after* items have loaded, the accumulated page is kept and
     * the failure surfaces here so the UI can show an inline retry row rather
     * than discarding a screen full of recordings.
     */
    fun observeLoadErrors(): Flow<ApiError?>

    suspend fun loadMore()

    /**
     * Re-fetch the newest segments.
     *
     * With [silent] `false` (pull-to-refresh) this resets to page 1: it
     * clears the accumulated list + cursor + exhaustion, emits
     * [PagedResult.Loading], then loads the first page, preserving the
     * active query.
     *
     * With [silent] `true` (the auto-refresh tick) nothing is cleared and no
     * [PagedResult.Loading] is emitted: page 1 is fetched and merged into the
     * accumulated list — new segments land at the front, already-loaded ones
     * are updated in place — so a screen refreshing every second neither
     * flashes a spinner, nor discards pages the user scrolled in, nor
     * replaces the list with an error when a tick fails.
     */
    suspend fun refresh(silent: Boolean = false)

    /**
     * Switch between browse and search mode. Search runs on the server
     * (transcript substring match), so it finds recordings that were never
     * paged in — which local filtering could not.
     *
     * Blank clears the query and returns to the paged browse list.
     */
    suspend fun setQuery(query: String)

    fun observeSegment(id: SegmentId): Flow<Outcome<SegmentDetails>>

    /** Rename. The server marks the title user-authored, so the auto-titler
     *  will never overwrite it. */
    suspend fun rename(id: SegmentId, title: String): Outcome<Segment>

    /** Delete the recording and everything derived from it. Fails with a
     *  [ApiError.Http] 409 while the segment is still recording. */
    suspend fun delete(id: SegmentId): Outcome<Unit>

    suspend fun waveform(id: SegmentId): Outcome<Waveform>

    suspend fun memories(id: SegmentId): Outcome<List<MemoryAtom>>

    /** The URL + auth header the audio player streams from. */
    suspend fun audioSource(id: SegmentId): Outcome<AudioSource>
}

/**
 * Production [SegmentRepository].
 *
 * Paging follows the same contract [SessionRepositoryImpl] established:
 *   - A non-empty page emits [PagedResult.Page] with the *accumulated* items.
 *   - An empty terminal page emits [PagedResult.Exhausted] only when nothing
 *     has accumulated; otherwise it re-emits the accumulated page with a null
 *     cursor, because mapping it to Exhausted would make the UI show "empty"
 *     and discard everything already loaded.
 *   - A failure emits an error *without* exhausting, so a retry re-fetches
 *     the same page.
 *
 * **Search mode is not paged.** The server returns one deduped row per
 * matching segment, capped, with a null cursor. So a query fetches exactly
 * once and [loadMore] is a no-op until the query is cleared — otherwise the
 * null cursor would read as "end of list" and immediately re-trigger a
 * browse-mode fetch that appended unrelated rows under the search results.
 *
 * Every fetch catches, re-throws [CancellationException], and maps other
 * throwables to a rendered state: no path throws out of a suspend function.
 */
class SegmentRepositoryImpl(
    private val api: SegmentApi,
    private val onUnknownKind: (String) -> Unit = {},
) : SegmentRepository {

    private val pages = MutableStateFlow<PagedResult<Segment>>(PagedResult.Loading)
    private val loadErrors = MutableStateFlow<ApiError?>(null)
    private val accumulated = mutableListOf<Segment>()
    private var cursor: String? = null
    private var started = false
    private var exhausted = false
    private var query: String = ""
    private val loadMutex = Mutex()

    override fun observeSegments(): Flow<PagedResult<Segment>> = pages.asStateFlow()

    override fun observeLoadErrors(): Flow<ApiError?> = loadErrors.asStateFlow()

    override suspend fun loadMore() = loadMutex.withLock {
        if (exhausted) return@withLock
        fetchPage()
    }

    override suspend fun refresh(silent: Boolean) = loadMutex.withLock {
        if (silent) return@withLock mergeFirstPage()
        reload()
    }

    override suspend fun setQuery(query: String) = loadMutex.withLock {
        val next = query.trim()
        if (next == this.query) return@withLock
        this.query = next
        reload()
    }

    /** Reset paging state and fetch page 1. Caller holds [loadMutex]. */
    private suspend fun reload() {
        accumulated.clear()
        cursor = null
        started = false
        exhausted = false
        loadErrors.value = null
        pages.value = PagedResult.Loading
        fetchPage()
    }

    /**
     * The auto-refresh tick: fetch page 1 (for the active query, if any) and
     * fold it into the list already on screen, without ever passing through
     * [PagedResult.Loading] or [PagedResult.Error].
     *
     * Merge policy — segments the server returns that we don't have yet are
     * prepended (the server orders newest-first); segments we already hold
     * are replaced by the fresh copy in place, so a still-recording segment's
     * duration and transcript count update without moving. Pages the user
     * scrolled in are untouched, and so are [cursor]/[exhausted].
     *
     * Failures are swallowed by design: this runs once a second, and a blip
     * must not replace the list with an error or flash an inline retry row.
     */
    private suspend fun mergeFirstPage() {
        val page = try {
            api.listSegments(
                limit = PAGE_SIZE,
                cursor = null,
                query = query.ifBlank { null },
                sessionId = null,
            )
        } catch (e: CancellationException) {
            throw e
        } catch (_: Throwable) {
            return
        }
        val fresh = page.segments.map { it.toDomain() }
        if (accumulated.isEmpty()) {
            // Nothing on screen yet: this tick IS the first page, so it owns
            // the paging state exactly as [fetchPage] would set it.
            started = true
            if (fresh.isEmpty() && page.nextCursor == null) {
                cursor = null
                exhausted = true
                pages.value = PagedResult.Exhausted
                return
            }
            accumulated.addAll(fresh)
            cursor = page.nextCursor
            exhausted = cursor == null || query.isNotEmpty()
            pages.value = PagedResult.Page(accumulated.toList(), cursor)
            return
        }
        val byId = fresh.associateBy { it.id }
        val known = accumulated.map { it.id }.toSet()
        val merged = fresh.filter { it.id !in known } +
            accumulated.map { byId[it.id] ?: it }
        accumulated.clear()
        accumulated.addAll(merged)
        pages.value = PagedResult.Page(accumulated.toList(), cursor)
    }

    private suspend fun fetchPage() {
        try {
            val page = api.listSegments(
                limit = PAGE_SIZE,
                cursor = if (started) cursor else null,
                query = query.ifBlank { null },
                sessionId = null,
            )
            started = true
            // Placed before the empty-terminal early return so that branch
            // also clears a stale error — otherwise a retry landing on an
            // empty terminal page would leave the error stuck AND set
            // `exhausted`, making Retry a permanent no-op.
            loadErrors.value = null
            val items = page.segments.map { it.toDomain() }
            if (items.isEmpty() && page.nextCursor == null) {
                if (accumulated.isEmpty()) {
                    pages.value = PagedResult.Exhausted
                } else {
                    cursor = null
                    pages.value = PagedResult.Page(accumulated.toList(), null)
                }
                exhausted = true
                return
            }
            accumulated.addAll(items)
            cursor = page.nextCursor
            pages.value = PagedResult.Page(accumulated.toList(), cursor)
            // Search results arrive whole and unpaged, so a query always ends
            // the list regardless of what the cursor says.
            if (cursor == null || query.isNotEmpty()) exhausted = true
        } catch (e: CancellationException) {
            throw e
        } catch (e: Throwable) {
            // Keep the list if we have one and surface the failure as an
            // inline retry; only a failed *initial* load replaces the screen.
            if (accumulated.isNotEmpty()) {
                loadErrors.value = httpApiError(e)
                pages.value = PagedResult.Page(accumulated.toList(), cursor)
            } else {
                pages.value = PagedResult.Error(e)
            }
        }
    }

    override fun observeSegment(id: SegmentId): Flow<Outcome<SegmentDetails>> = flow {
        emit(
            attempt { api.getSegment(id).toDomain(onUnknownKind) },
        )
    }

    override suspend fun rename(id: SegmentId, title: String): Outcome<Segment> {
        val result = attempt { api.renameSegment(id, title).toDomain() }
        // A rename has to be reflected in the list too: the row the user just
        // retitled is on screen behind the detail sheet, and re-fetching the
        // whole list to learn one string would throw away their scroll.
        if (result is Outcome.Success) replaceInPage(result.value)
        return result
    }

    override suspend fun delete(id: SegmentId): Outcome<Unit> {
        val result = attempt { api.deleteSegment(id) }
        if (result is Outcome.Success) removeFromPage(id)
        return result
    }

    override suspend fun waveform(id: SegmentId): Outcome<Waveform> =
        attempt { api.getSegmentWaveform(id).toDomain() }

    override suspend fun memories(id: SegmentId): Outcome<List<MemoryAtom>> =
        attempt { api.getSegmentMemory(id).atoms.map { it.toMemoryAtom() } }

    override suspend fun audioSource(id: SegmentId): Outcome<AudioSource> =
        attempt { api.audioSource(id) }

    /** Run a call, classifying any failure into an [ApiError]. Cancellation
     *  propagates; nothing else escapes. */
    private suspend fun <T> attempt(block: suspend () -> T): Outcome<T> = try {
        Outcome.Success(block())
    } catch (e: CancellationException) {
        throw e
    } catch (e: Throwable) {
        Outcome.Failure(httpApiError(e))
    }

    private fun replaceInPage(updated: Segment) {
        val index = accumulated.indexOfFirst { it.id == updated.id }
        if (index < 0) return
        // The list row carries a memory count and a match snippet that the
        // PATCH response doesn't recompute, so keep the row's own values and
        // take only what the rename actually changed.
        accumulated[index] = accumulated[index].copy(title = updated.title)
        republish()
    }

    private fun removeFromPage(id: SegmentId) {
        if (!accumulated.removeAll { it.id == id }) return
        republish()
    }

    private fun republish() {
        if (pages.value is PagedResult.Page) {
            pages.value = PagedResult.Page(accumulated.toList(), cursor)
        }
    }
}
