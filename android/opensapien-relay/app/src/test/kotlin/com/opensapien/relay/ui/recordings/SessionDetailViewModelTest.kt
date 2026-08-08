package com.opensapien.relay.ui.recordings

import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.model.PagedResult
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.data.SpeakerActions
import com.opensapien.relay.data.SpeakerCache
import com.opensapien.relay.data.SessionRepository
import com.opensapien.relay.domain.model.CaptureEvent
import com.opensapien.relay.domain.model.SessionDetails
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.domain.model.TranscriptChunk
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
import kotlin.test.assertNotNull
import kotlin.test.assertNull
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

    /** Captures nameSpeaker/reassignSpeaker calls so the VM tests can assert
     *  the control was emitted with the right ids/scope — without a socket. */
    private class RecordingSpeakerActions : SpeakerActions {
        data class Named(val sessionId: String, val speakerId: String, val name: String)
        data class Reassigned(val sessionId: String, val fromId: String, val toId: String, val scope: String)

        val named = mutableListOf<Named>()
        val reassigned = mutableListOf<Reassigned>()

        override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
            named += Named(sessionId, speakerId, name)
        }

        override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
            reassigned += Reassigned(sessionId, fromId, toId, scope)
        }
    }

    @Test fun renameSpeakerEmitsNameSpeakerControlAndUpdatesCacheOptimistically() = runTest(dispatcher) {
        val cache = SpeakerCache()
        // The hop had no name yet; the cache knows the speaker is the wearer.
        cache.upsert("spk-1", null, isWearer = true)
        val actions = RecordingSpeakerActions()
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val eventsFlow = MutableStateFlow<Outcome<List<CaptureEvent>>>(Outcome.Success(emptyList()))
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, eventsFlow),
            speakerCache = cache,
            speakerActions = actions,
        )

        vm.renameSpeaker("spk-1", "Sarah")
        testScheduler.advanceUntilIdle()

        // Optimistic cache update: the label renders immediately.
        assertEquals("Sarah", cache.get("spk-1")?.name)
        assertEquals(true, cache.get("spk-1")?.isWearer, "rename preserves isWearer")
        // Control emitted with the VM's session id + speaker id + new name.
        assertEquals(1, actions.named.size)
        assertEquals("s1", actions.named[0].sessionId)
        assertEquals("spk-1", actions.named[0].speakerId)
        assertEquals("Sarah", actions.named[0].name)
    }

    @Test fun reassignSpeakerEmitsReassignControlWithScopeAll() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("spk-1", "Sarah", isWearer = false)
        cache.upsert("spk-2", "Lee", isWearer = true)
        val actions = RecordingSpeakerActions()
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val eventsFlow = MutableStateFlow<Outcome<List<CaptureEvent>>>(Outcome.Success(emptyList()))
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, eventsFlow),
            speakerCache = cache,
            speakerActions = actions,
        )

        vm.reassignSpeaker("spk-1", "spk-2")
        testScheduler.advanceUntilIdle()

        assertEquals(1, actions.reassigned.size)
        assertEquals("s1", actions.reassigned[0].sessionId)
        assertEquals("spk-1", actions.reassigned[0].fromId)
        assertEquals("spk-2", actions.reassigned[0].toId)
        assertEquals("all", actions.reassigned[0].scope, "v1 uses scope=all (session-scoped server)")
    }

    /** A [SpeakerActions] whose calls throw, to exercise the revert path. */
    private class ThrowingSpeakerActions : SpeakerActions {
        override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) =
            error("rename failed")
        override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) =
            error("reassign failed")
    }

    @Test fun renameSpeakerRevertsCacheOnFailure() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("spk-1", "Old", isWearer = false)
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val eventsFlow = MutableStateFlow<Outcome<List<CaptureEvent>>>(Outcome.Success(emptyList()))
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, eventsFlow),
            speakerCache = cache,
            speakerActions = ThrowingSpeakerActions(),
        )

        vm.renameSpeaker("spk-1", "New")
        testScheduler.advanceUntilIdle()

        // The optimistic "New" was reverted to the prior "Old" on failure.
        assertEquals("Old", cache.get("spk-1")?.name, "cache reverted to prior name on failure")
        // The error banner surfaces.
        assertNotNull(vm.speakerError.value, "speakerError set on failure")
    }

    @Test fun reassignSpeakerRefreshesOnSuccess() = runTest(dispatcher) {
        // onRefresh re-collects the one-shot flows; with MutableStateFlow each
        // (re-)collection replays the current value. Updating the events value
        // before the reassign means a successful re-fetch (onRefresh fired)
        // surfaces the new events in the UI state.
        //
        // NOTE: asserting events.size 1→2 alone is TAUTOLOGICAL here — the
        // revision=0 combine collector is still active, so a hot
        // MutableStateFlow eventsFlow propagates the new value to vm.state
        // WHETHER OR NOT onRefresh fires. The revision assertion below is the
        // load-bearing check: it bumps 0→1 only if reassignSpeaker's
        // onSuccess { onRefresh() } actually ran.
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val eventsFlow = MutableStateFlow<Outcome<List<CaptureEvent>>>(
            Outcome.Success(listOf(chunk("e1", 1))),
        )
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, eventsFlow),
            speakerActions = RecordingSpeakerActions(),
        )
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        val before = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals(1, before.events.size)
        assertEquals(0, vm.revisionValue.value, "revision starts at 0 before any refresh")

        // New server data the re-fetch should pick up.
        eventsFlow.value = Outcome.Success(listOf(chunk("e1", 1), chunk("e2", 2)))
        vm.reassignSpeaker("spk-1", "spk-2")
        testScheduler.advanceUntilIdle()

        val after = assertIs<SessionDetailUiState.Loaded>(vm.state.value)
        assertEquals(2, after.events.size, "onRefresh re-fetched the events after a successful reassign")
        // The load-bearing assertion: onRefresh fired (bumped revision 0→1).
        // This FAILS if onSuccess { onRefresh() } is deleted, even though the
        // hot-flow events-size assertion above would still pass.
        assertEquals(1, vm.revisionValue.value, "reassign on success bumped the refresh revision")
        assertNull(vm.speakerError.value, "no error on success")
    }

    @Test fun youConfirmationPromptsOnFirstWearerYouTranscript() = runTest(dispatcher) {
        val cache = SpeakerCache()
        val actions = RecordingSpeakerActions()
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val events = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, events),
            speakerCache = cache,
            speakerActions = actions,
        )
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        events.emit(Outcome.Success(listOf(
            TranscriptChunk(
                id = "e1", sessionId = SessionId("s1"), seq = 1, startMs = 1000L,
                createdAt = Instant.EPOCH, text = "t", durationMs = 500,
                speaker = "you", speakerName = "You", isWearer = true,
            ),
        )))
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Prompting("you"), vm.youConfirmation.value)

        vm.confirmYou("Kevin")
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Done, vm.youConfirmation.value)
        assertEquals("Kevin", cache.get("you")?.name, "confirmYou renames the wearer in the cache")
        assertEquals(1, actions.named.size, "confirmYou emits a name_speaker control")
        assertEquals("you", actions.named[0].speakerId)
        assertEquals("Kevin", actions.named[0].name)
    }

    @Test fun youConfirmationDoesNotPromptWhenAlreadyNamed() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("you", "Kevin", isWearer = true)
        val actions = RecordingSpeakerActions()
        val summaryFlow = MutableStateFlow<Outcome<SessionDetails>>(
            Outcome.Success(SessionDetails(summary("s1"), emptyList())),
        )
        val events = MutableSharedFlow<Outcome<List<CaptureEvent>>>(extraBufferCapacity = 8)
        val vm = SessionDetailViewModel(
            id = SessionId("s1"),
            repo = FakeDetailRepo(summaryFlow, events),
            speakerCache = cache,
            speakerActions = actions,
        )
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        events.emit(Outcome.Success(listOf(
            TranscriptChunk(
                id = "e1", sessionId = SessionId("s1"), seq = 1, startMs = 1000L,
                createdAt = Instant.EPOCH, text = "t", durationMs = 500,
                speaker = "you", speakerName = "Kevin", isWearer = true,
            ),
        )))
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Idle, vm.youConfirmation.value)
    }
}
