package com.openrecall.relay.data

import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.model.PagedResult
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentDetails
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.domain.model.Waveform
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flowOf
import java.time.Instant

/**
 * Scripted [SegmentRepository] for ViewModel tests.
 *
 * Paging mirrors [SegmentRepositoryImpl]'s contract: queued pages are served
 * one [loadMore] at a time, an empty terminal page exhausts the list, and
 * [setQuery] replays the scripted pages from the start (search results are
 * whatever the next queued page holds).
 */
class FakeSegmentRepository : SegmentRepository {

    data class QueuedPage(val items: List<Segment>, val nextCursor: String?)

    private val queued = ArrayDeque<QueuedPage>()
    private val queuedOriginal = mutableListOf<QueuedPage>()
    private val accumulated = mutableListOf<Segment>()
    private var exhausted = false

    private val pages = MutableStateFlow<PagedResult<Segment>>(PagedResult.Loading)
    private val loadErrors = MutableStateFlow<ApiError?>(null)

    /** Test-only: how many times [refresh] has been called. */
    var refreshCount = 0
        private set

    /** Test-only: the query last pushed through [setQuery]. */
    var lastQuery: String? = null
        private set

    /** Detail payload served by [observeSegment], keyed by id. */
    val details = mutableMapOf<String, Outcome<SegmentDetails>>()

    var renamed: Pair<String, String>? = null
        private set
    var deletedId: String? = null
        private set

    fun queue(items: List<Segment>, nextCursor: String?) {
        val page = QueuedPage(items, nextCursor)
        queued.addLast(page)
        queuedOriginal.add(page)
    }

    override fun observeSegments(): Flow<PagedResult<Segment>> = pages.asStateFlow()

    override fun observeLoadErrors(): Flow<ApiError?> = loadErrors.asStateFlow()

    override suspend fun loadMore() {
        if (exhausted) return
        if (queued.isEmpty()) {
            if (accumulated.isEmpty()) pages.value = PagedResult.Exhausted
            exhausted = true
            return
        }
        val (items, cursor) = queued.removeFirst()
        if (items.isEmpty() && cursor == null) {
            pages.value = PagedResult.Exhausted
            exhausted = true
            return
        }
        accumulated.addAll(items)
        pages.value = PagedResult.Page(accumulated.toList(), cursor)
        if (cursor == null) exhausted = true
    }

    /**
     * Re-serve the scripted pages from the start. [silent] only suppresses
     * the [PagedResult.Loading] emission — the fake has no server to diff
     * against, so it re-plays its queue either way (the real impl merges;
     * see [SegmentRepositoryImpl]).
     */
    override suspend fun refresh(silent: Boolean) {
        refreshCount++
        reload(silent)
    }

    override suspend fun setQuery(query: String) {
        lastQuery = query
        reload(silent = false)
    }

    private suspend fun reload(silent: Boolean) {
        accumulated.clear()
        exhausted = false
        queued.clear()
        queued.addAll(queuedOriginal)
        if (!silent) pages.value = PagedResult.Loading
        if (queuedOriginal.isEmpty()) return
        loadMore()
    }

    override fun observeSegment(id: SegmentId): Flow<Outcome<SegmentDetails>> = flowOf(
        details[id.value] ?: Outcome.Failure(ApiError.Http(404)),
    )

    override suspend fun rename(id: SegmentId, title: String): Outcome<Segment> {
        renamed = id.value to title
        val current = (details[id.value] as? Outcome.Success)?.value?.summary
            ?: return Outcome.Failure(ApiError.Http(404))
        val updated = current.copy(title = title)
        details[id.value] = Outcome.Success(
            SegmentDetails(
                summary = updated,
                events = (details[id.value] as Outcome.Success).value.events,
            ),
        )
        return Outcome.Success(updated)
    }

    /** Overridable per-test: a 409 is the "still recording" refusal. */
    var deleteResult: Outcome<Unit> = Outcome.Success(Unit)

    override suspend fun delete(id: SegmentId): Outcome<Unit> {
        deletedId = id.value
        return deleteResult
    }

    var waveformResult: Outcome<Waveform> = Outcome.Failure(ApiError.Http(404))
    override suspend fun waveform(id: SegmentId): Outcome<Waveform> = waveformResult

    var memoriesResult: Outcome<List<MemoryAtom>> = Outcome.Success(emptyList())
    override suspend fun memories(id: SegmentId): Outcome<List<MemoryAtom>> = memoriesResult

    var audioResult: Outcome<AudioSource> = Outcome.Failure(ApiError.Http(404))
    override suspend fun audioSource(id: SegmentId): Outcome<AudioSource> = audioResult
}

/** A minimal [Segment] for tests; override only what the assertion cares about. */
fun testSegment(
    id: String,
    title: String? = null,
    startedAt: Instant = Instant.EPOCH,
    durationMs: Long = 1_000,
    transcriptCount: Int = 1,
    memoryCount: Int = 0,
    preview: String = "preview-$id",
    hasAudio: Boolean = false,
    closed: Boolean = true,
    matchSnippet: String? = null,
) = Segment(
    id = SegmentId(id),
    sessionId = SessionId(id.substringBefore(':')),
    title = title,
    startedAt = startedAt,
    endedAt = startedAt.plusMillis(durationMs),
    durationMs = durationMs,
    transcriptCount = transcriptCount,
    memoryCount = memoryCount,
    preview = preview,
    hasAudio = hasAudio,
    closed = closed,
    matchSnippet = matchSnippet,
)
