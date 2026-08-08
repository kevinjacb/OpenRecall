package com.opensapien.relay.data

import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.model.PagedResult
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.domain.model.CaptureEvent
import com.opensapien.relay.domain.model.SessionDetails
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.test.runTest
import java.time.Instant
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertIs
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pins the paged-list contract of [SessionRepository] via the
 * in-memory fake the rest of Phase 2/4/5 uses as a test fixture:
 *   - The first emit of [observeSessions] is [PagedResult.Loading].
 *   - After a successful [loadMoreSessions], the StateFlow's current
 *     value is a [PagedResult.Page] with all accumulated items.
 *   - When the server's cursor on the last page is null, the next
 *     [loadMoreSessions] flips the state to [PagedResult.Exhausted]
 *     and subsequent calls are no-ops.
 *   - [observeSession] / [observeSessionEvents] emit an
 *     [Outcome.Failure] (ApiError.Unreachable) until Phase 5 wires
 *     the real OpenSapienHttpClient call.
 *
 * Tests read the StateFlow's current value (`.first()` on a StateFlow
 * returns the latest value) — they do not depend on emission history,
 * which is correct for a StateFlow-based contract.
 */
class FakeSessionRepositoryTest {

    private fun summary(id: String, startedAt: Instant = Instant.parse("2026-07-04T10:00:00Z")) =
        SessionSummary(
            id = SessionId(id),
            startedAt = startedAt,
            endedAt = startedAt.plusSeconds(60),
            durationMs = 60_000,
            transcriptCount = 1,
            preview = "hi",
        )

    @Test fun initialEmitIsLoading() = runTest {
        val repo = FakeSessionRepository()
        assertIs<PagedResult.Loading>(repo.observeSessions().first())
    }

    @Test fun loadMoreEmitsPageWithItemsAndNextCursor() = runTest {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("s1"), summary("s2")), nextCursor = "cur-1")
        repo.queue(listOf(summary("s3")), nextCursor = "cur-2")

        assertIs<PagedResult.Loading>(repo.observeSessions().first())

        repo.loadMoreSessions()
        val s1 = repo.observeSessions().first()
        val page1 = assertIs<PagedResult.Page<SessionSummary>>(s1)
        assertEquals(listOf("s1", "s2"), page1.items.map { it.id.value })
        assertEquals("cur-1", page1.nextCursor)

        repo.loadMoreSessions()
        val s2 = repo.observeSessions().first()
        val page2 = assertIs<PagedResult.Page<SessionSummary>>(s2)
        assertEquals(listOf("s1", "s2", "s3"), page2.items.map { it.id.value })
        assertEquals("cur-2", page2.nextCursor)
    }

    @Test fun loadMoreExhaustsAfterTerminalNullCursor() = runTest {
        // Server signals "end of list" by sending a page with
        // nextCursor == null. The next loadMoreSessions flips the
        // StateFlow to PagedResult.Exhausted.
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("s1")), nextCursor = "cur-1")
        repo.queue(listOf(summary("s2")), nextCursor = null)

        repo.loadMoreSessions()
        val s1 = repo.observeSessions().first()
        val page1 = assertIs<PagedResult.Page<SessionSummary>>(s1)
        assertEquals(listOf("s1"), page1.items.map { it.id.value })
        assertEquals("cur-1", page1.nextCursor)

        repo.loadMoreSessions()
        val s2 = repo.observeSessions().first()
        // The second page had a non-empty list but a null cursor, so
        // the data is still on the wire and we transition to Exhausted
        // — the data is what consumers want to render.
        val page2 = assertIs<PagedResult.Page<SessionSummary>>(s2)
        assertEquals(listOf("s1", "s2"), page2.items.map { it.id.value })
        assertNull(page2.nextCursor)
    }

    @Test fun emptyTerminalPageExhaustsImmediately() = runTest {
        // An empty list with a null cursor is the server's "no data
        // at all" signal. The fake should emit Exhausted.
        val repo = FakeSessionRepository()
        repo.queue(emptyList(), nextCursor = null)
        repo.loadMoreSessions()
        val s = repo.observeSessions().first()
        assertIs<PagedResult.Exhausted>(s)
    }

    @Test fun loadMoreIsIdempotentAfterExhausted() = runTest {
        // Once the StateFlow is Exhausted, additional loadMore calls
        // do nothing — the consumer has the answer, the repo is quiet.
        val repo = FakeSessionRepository()
        repo.queue(emptyList(), nextCursor = null)
        repo.loadMoreSessions()
        assertIs<PagedResult.Exhausted>(repo.observeSessions().first())
        repo.loadMoreSessions()
        repo.loadMoreSessions()
        assertIs<PagedResult.Exhausted>(repo.observeSessions().first())
    }

    @Test fun observeSessionEmitsFailure() = runTest {
        val repo = FakeSessionRepository()
        val out = repo.observeSession(SessionId("s1")).first()
        val fail = assertIs<Outcome.Failure>(out)
        val err = fail.error
        assertIs<ApiError.Unreachable>(err)
        assertTrue(err.reason.contains("not yet wired"))
    }

    @Test fun observeSessionEventsEmitsFailure() = runTest {
        val repo = FakeSessionRepository()
        val out: Outcome<List<CaptureEvent>> = repo.observeSessionEvents(SessionId("s1")).first()
        val fail = assertIs<Outcome.Failure>(out)
        assertIs<ApiError.Unreachable>(fail.error)
    }

    @Test fun observeSessionFailureIsStableAcrossSubscribers() = runTest {
        // Two independent collectors both see the same failure.
        val repo = FakeSessionRepository()
        val a = repo.observeSession(SessionId("s1")).first()
        val b = repo.observeSession(SessionId("s1")).first()
        assertEquals((a as Outcome.Failure).error, (b as Outcome.Failure).error)
    }

    /**
     * Sanity check: the [FakeSessionRepository] implements the
     * [SessionRepository] interface (compile-time), and the paged-list
     * emits the states the contract requires. Phase 4/5 ViewModel
     * tests will reuse this fake.
     */
    @Test fun fakeIsASessionRepository() {
        val repo: SessionRepository = FakeSessionRepository()
        assertTrue(repo is FakeSessionRepository)
    }

    @Suppress("unused") // forces SessionDetails import to be considered
    private fun unused(_d: SessionDetails) = Unit
}
