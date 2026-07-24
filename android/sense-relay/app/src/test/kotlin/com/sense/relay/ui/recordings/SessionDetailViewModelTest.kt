package com.sense.relay.ui.recordings

import com.sense.relay.core.model.ApiError
import com.sense.relay.core.model.PagedResult
import com.sense.relay.core.result.Outcome
import com.sense.relay.data.SessionRepository
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.domain.model.TranscriptChunk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableSharedFlow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.flowOf
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.launch
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import java.time.Instant
import kotlin.test.AfterTest
import kotlin.test.BeforeTest
import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertIs
import kotlin.test.assertTrue

/**
 * Pins [SessionDetailViewModel]'s progressive emission: the summary lands
 * first ([SessionDetailUiState.LoadedSummary]), then the events fill in
 * ([SessionDetailUiState.Loaded]); a summary failure is
 * [SessionDetailUiState.Failed], while an events failure keeps the summary.
 *
 * Note on the multi-batch fake: the production [SessionRepositoryImpl] emits
 * events ONCE (a single `GET /sessions/{id}/events` fetch), so the two-batch
 * sequence below is a defensive exercise of the reduce over multiple
 * emissions — it pins that each emission REPLACES (not appends) and stays in
 * seq order, not that the production repo streams batches.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SessionDetailViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)
    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private class FakeDetailRepo(
        private val summaryFlow: Flow<Outcome<SessionDetails>>,
        private val eventsFlow: Flow<Outcome<List<CaptureEvent>>>,
    ) : SessionRepository {
        override fun observeSessions() = flowOf<PagedResult<SessionSummary>>(PagedResult.Loading)
        override suspend fun loadMoreSessions() {}
        override fun observeSession(id: SessionId) = summaryFlow
        override fun observeSessionEvents(id: SessionId) = eventsFlow
    }

    private fun summary(id: String) = SessionSummary(
        id = SessionId(id),
        startedAt = Instant.EPOCH,
        endedAt = null,
        durationMs = 1000,
        transcriptCount = 1,
        preview = "p-$id",
    )

    private fun chunk(id: String, seq: Int): CaptureEvent = TranscriptChunk(
        id = id,
        sessionId = SessionId("s1"),
        seq = seq,
        startMs = seq * 1000L,
        createdAt = Instant.EPOCH,
        text = "t-$id",
        durationMs = 500,
    )

    @Test fun emitsSummaryFirstThenEventBatches() = runTest(dispatcher) {
        val details = SessionDetails(summary("s1"), emptyList())
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(Outcome.Success(details))
        val eventsFlow = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, eventsFlow))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        // Summary present, events pending (onStart null) → LoadedSummary.
        val summaryOnly = assertIs<SessionDetailUiState.LoadedSummary>(vm.state.value)
        assertEquals("s1", summaryOnly.summary.id.value)

        eventsFlow.emit(Outcome.Success(listOf(chunk("e1", 1))))
        testScheduler.advanceUntilIdle()
        val batch1 = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals(1, batch1.events.size)

        eventsFlow.emit(Outcome.Success(listOf(chunk("e1", 1), chunk("e2", 2))))
        testScheduler.advanceUntilIdle()
        val batch2 = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals(2, batch2.events.size)
    }

    @Test fun summaryFailureIsFailed() = runTest(dispatcher) {
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Failure(ApiError.Unreachable("offline")),
        )
        val eventsFlow = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, eventsFlow))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val failed = assertIs<SessionDetailUiState.Failed>(vm.state.value)
        // The reason is the ErrorMapper's consumer-facing string, not the
        // raw ApiError reason ("offline").
        assertEquals("Can't reach the server", failed.reason)
    }

    @Test fun eventsFailureKeepsLoadedSummary() = runTest(dispatcher) {
        val details = SessionDetails(summary("s1"), emptyList())
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(Outcome.Success(details))
        val eventsFlow = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, eventsFlow))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertIs<SessionDetailUiState.LoadedSummary>(vm.state.value)

        eventsFlow.emit(Outcome.Failure(ApiError.Unreachable("events down")))
        testScheduler.advanceUntilIdle()
        // Summary still stands; the timeline just doesn't fill in.
        assertIs<SessionDetailUiState.LoadedSummary>(vm.state.value)
    }

    @Test fun onRefreshReFetchesSummaryAndEventsAndTogglesIsRefreshing() = runTest(dispatcher) {
        // The per-session flows are one-shot; onRefresh bumps a revision that
        // re-collects them from scratch, so new server data lands. Using
        // MutableStateFlow for both means each (re-)collection replays the
        // current value — updating the value before the refresh simulates
        // new server data the re-fetch picks up.
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val eventsFlow = MutableStateFlow<Outcome<List<CaptureEvent>>>(
            Outcome.Success(listOf(chunk("e1", 1))),
        )
        val vm = SessionDetailViewModel(SessionId("s1"), FakeDetailRepo(summaryFlow, eventsFlow))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        val before = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals(1, before.events.size)
        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")

        // New server data; a pull-to-refresh re-fetches.
        summaryFlow.value = Outcome.Success(SessionDetails(summary("s1-v2"), emptyList()))
        eventsFlow.value = Outcome.Success(listOf(chunk("e1", 1), chunk("e2", 2)))
        vm.onRefresh()
        assertTrue(vm.isRefreshing.value, "refresh flag set immediately")
        testScheduler.advanceUntilIdle()

        val after = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals("s1-v2", after.summary.id.value, "summary re-fetched")
        assertEquals(2, after.events.size, "events re-fetched")
        assertFalse(vm.isRefreshing.value, "refresh flag cleared after the re-fetch")
    }
}
