package com.sense.relay.data

import com.sense.relay.core.model.PagedResult
import com.sense.relay.core.result.Outcome
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.domain.model.TranscriptChunk
import com.sense.relay.http.dto.CaptureEventDto
import com.sense.relay.http.dto.SessionDetailsDto
import com.sense.relay.http.dto.SessionSummaryDto
import com.sense.relay.http.dto.SessionsPageDto
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import java.io.IOException
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Pins [SessionRepositoryImpl] against a fake [SessionApi]: the cursor
 * pagination semantics (which mirror [FakeSessionRepository] exactly), the
 * failure→Error→recovery path, and the DTO→domain mapping (summary + events,
 * sorted by seq, unknown kinds dropped).
 */
class SessionRepositoryImplTest {

    private class FakeSessionApi : SessionApi {
        val queued = ArrayDeque<SessionsPageDto>()
        val listCalls = mutableListOf<Pair<Int, String?>>()
        var listError: Throwable? = null
        var detail: SessionDetailsDto? = null
        var detailError: Throwable? = null
        var events: List<CaptureEventDto> = emptyList()
        var eventsError: Throwable? = null

        override suspend fun listSessions(limit: Int, cursor: String?): SessionsPageDto {
            listCalls.add(limit to cursor)
            listError?.let { throw it }
            return queued.removeFirst()
        }

        override suspend fun getSession(id: SessionId): SessionDetailsDto {
            detailError?.let { throw it }
            return detail!!
        }

        override suspend fun getSessionEvents(id: SessionId): List<CaptureEventDto> {
            eventsError?.let { throw it }
            return events
        }
    }

    private fun summaryDto(id: String) = SessionSummaryDto(
        id = id,
        startedAt = "2026-07-05T00:00:00Z",
        endedAt = null,
        durationMs = 1000,
        transcriptCount = 1,
        preview = "p-$id",
    )

    private fun page(ids: List<String>, cursor: String?) =
        SessionsPageDto(sessions = ids.map { summaryDto(it) }, nextCursor = cursor)

    private fun eventDto(id: String, seq: Int, kind: String = "transcript") = CaptureEventDto(
        id = id,
        sessionId = "s1",
        seq = seq,
        startMs = seq * 1000L,
        createdAt = "2026-07-05T00:00:0${seq}Z",
        kind = kind,
        text = "t-$id",
    )

    private suspend fun SessionRepository.current() = observeSessions().first()

    @Test fun firstLoadFetchesPageOneWithNullCursor() = runTest {
        val api = FakeSessionApi().apply { queued.add(page(listOf("a", "b"), "c1")) }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        val page = assertIs<PagedResult.Page<SessionSummary>>(repo.current())
        assertEquals(2, page.items.size)
        assertEquals("c1", page.nextCursor)
        assertEquals(listOf<Pair<Int, String?>>(20 to null), api.listCalls)
    }

    @Test fun secondLoadAppendsUsingTheCursor() = runTest {
        val api = FakeSessionApi().apply {
            queued.add(page(listOf("a", "b"), "c1"))
            queued.add(page(listOf("c", "d"), null))
        }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        repo.loadMoreSessions()
        val page = assertIs<PagedResult.Page<SessionSummary>>(repo.current())
        assertEquals(4, page.items.size)
        assertEquals(null, page.nextCursor)
        assertEquals(listOf(20 to null, 20 to "c1"), api.listCalls)
    }

    @Test fun nullCursorPageMakesFurtherLoadsNoOps() = runTest {
        val api = FakeSessionApi().apply { queued.add(page(listOf("a"), null)) }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        repo.loadMoreSessions() // exhausted: must not fetch again
        assertEquals(1, api.listCalls.size)
        val page = assertIs<PagedResult.Page<SessionSummary>>(repo.current())
        assertEquals(1, page.items.size)
    }

    @Test fun emptyTerminalPageEmitsExhausted() = runTest {
        val api = FakeSessionApi().apply { queued.add(page(emptyList(), null)) }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        assertIs<PagedResult.Exhausted>(repo.current())
    }

    @Test fun emptyTerminalPageAfterDataKeepsPageWithNullCursor() = runTest {
        // Edge case the cross-review surfaced: page 1 has items + a cursor,
        // page 2 is the server's "nothing more" signal (empty + null). The
        // Impl must NOT leave the previous Page's cursor dangling (the UI
        // would show canLoadMore=true while loadMore is a no-op), and must
        // NOT emit Exhausted (that would discard the loaded sessions via
        // RecordingsUiState.Empty). It re-emits the accumulated Page with a
        // null cursor.
        val api = FakeSessionApi().apply {
            queued.add(page(listOf("a", "b"), "c1"))
            queued.add(page(emptyList(), null))
        }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        repo.loadMoreSessions()
        val p = assertIs<PagedResult.Page<SessionSummary>>(repo.current())
        assertEquals(listOf("a", "b"), p.items.map { it.id.value })
        assertEquals(null, p.nextCursor, "cursor must be cleared so the UI sees canLoadMore=false")

        // A further load is a no-op (exhausted).
        repo.loadMoreSessions()
        assertEquals(2, api.listCalls.size)
    }

    @Test fun fetchFailureEmitsErrorThenRecovers() = runTest {
        val api = FakeSessionApi().apply { listError = IOException("down") }
        val repo = SessionRepositoryImpl(api)
        repo.loadMoreSessions()
        assertIs<PagedResult.Error>(repo.current())

        // Recovery: clear the error, queue a page; the next load retries page 1
        // (cursor still null because the failure didn't advance state).
        api.listError = null
        api.queued.add(page(listOf("a"), null))
        repo.loadMoreSessions()
        val page = assertIs<PagedResult.Page<SessionSummary>>(repo.current())
        assertEquals(1, page.items.size)
        assertEquals(listOf<Pair<Int, String?>>(20 to null, 20 to null), api.listCalls)
    }

    @Test fun observeSessionMapsSummaryAndEventsSortedBySeq() = runTest {
        val api = FakeSessionApi().apply {
            detail = SessionDetailsDto(
                summary = summaryDto("s1"),
                events = listOf(eventDto("e2", seq = 2), eventDto("e1", seq = 1)),
            )
        }
        val repo = SessionRepositoryImpl(api)
        val out = assertIs<Outcome.Success<*>>(repo.observeSession(SessionId("s1")).first())
        val details = out.value as com.sense.relay.domain.model.SessionDetails
        assertEquals("s1", details.summary.id.value)
        assertEquals(listOf(1, 2), details.events.map { it.seq })
    }

    @Test fun observeSessionFailureMapsToOutcomeFailure() = runTest {
        val api = FakeSessionApi().apply { detailError = IOException("nope") }
        val repo = SessionRepositoryImpl(api)
        assertIs<Outcome.Failure>(repo.observeSession(SessionId("s1")).first())
    }

    @Test fun observeSessionEventsMapsSortsAndDropsUnknownKinds() = runTest {
        var dropped = 0
        val api = FakeSessionApi().apply {
            events = listOf(
                eventDto("e2", seq = 2),
                eventDto("e1", seq = 1),
                eventDto("e3", seq = 3, kind = "image"), // unknown → dropped
            )
        }
        val repo = SessionRepositoryImpl(api, onUnknownKind = { dropped++ })
        val out = assertIs<Outcome.Success<*>>(repo.observeSessionEvents(SessionId("s1")).first())
        @Suppress("UNCHECKED_CAST")
        val events = out.value as List<TranscriptChunk>
        assertEquals(listOf(1, 2), events.map { it.seq })
        assertEquals(1, dropped)
    }

    @Test fun observeSessionEventsFailureMapsToOutcomeFailure() = runTest {
        val api = FakeSessionApi().apply { eventsError = IOException("nope") }
        val repo = SessionRepositoryImpl(api)
        assertIs<Outcome.Failure>(repo.observeSessionEvents(SessionId("s1")).first())
    }

    @Test fun securityExceptionMapsToUnauthorized() = runTest {
        val api = FakeSessionApi().apply { detailError = SecurityException("401") }
        val repo = SessionRepositoryImpl(api)
        val failure = assertIs<Outcome.Failure>(repo.observeSession(SessionId("s1")).first())
        assertTrue(failure.error is com.sense.relay.core.model.ApiError.Unauthorized)
    }
}
