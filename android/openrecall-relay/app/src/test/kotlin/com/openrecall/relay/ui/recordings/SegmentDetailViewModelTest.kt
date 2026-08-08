package com.openrecall.relay.ui.recordings

import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.data.FakeSegmentRepository
import com.openrecall.relay.data.SpeakerActions
import com.openrecall.relay.data.SpeakerCache
import com.openrecall.relay.data.testSegment
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.SegmentDetails
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.domain.model.TranscriptChunk
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
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
 * Pins [SegmentDetailViewModel].
 *
 * `GET /segments/{id}` returns the summary and the transcript together, so
 * there is one state to reduce rather than the old two-phase
 * summary-then-events dance.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SegmentDetailViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)
    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private val id = SegmentId("s1:1")

    private fun chunk(
        eventId: String,
        seq: Int,
        speaker: String? = null,
        speakerName: String? = null,
        isWearer: Boolean = false,
    ): CaptureEvent = TranscriptChunk(
        id = eventId,
        sessionId = SessionId("s1"),
        seq = seq,
        startMs = seq * 1000L,
        createdAt = Instant.EPOCH,
        text = "t-$eventId",
        durationMs = 500,
        speaker = speaker,
        speakerName = speakerName,
        isWearer = isWearer,
    )

    private fun repoWith(
        events: List<CaptureEvent> = emptyList(),
        title: String? = null,
    ): FakeSegmentRepository = FakeSegmentRepository().apply {
        details[id.value] = Outcome.Success(
            SegmentDetails(testSegment(id.value, title = title), events),
        )
    }

    @Test fun loadsSummaryAndTranscriptTogether() = runTest(dispatcher) {
        val repo = repoWith(events = listOf(chunk("e1", 1), chunk("e2", 2)))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<SegmentDetailUiState.Loaded>(vm.state.value)
        assertEquals("s1:1", loaded.summary.id.value)
        assertEquals(2, loaded.events.size)
    }

    @Test fun fetchFailureIsFailedWithAConsumerFacingReason() = runTest(dispatcher) {
        val repo = FakeSegmentRepository() // no detail scripted → 404
        repo.details[id.value] = Outcome.Failure(ApiError.Unreachable("offline"))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val failed = assertIs<SegmentDetailUiState.Failed>(vm.state.value)
        // The ErrorMapper's user-facing string, not the raw ApiError reason.
        assertEquals("Can't reach the server", failed.reason)
    }

    @Test fun missingAudioLeavesThePlayerEmptyWithoutAnError() = runTest(dispatcher) {
        // A recording with no audio is normal — retention sweeps audio while
        // keeping the transcript — so it must not surface as a failure.
        val repo = repoWith(events = listOf(chunk("e1", 1)))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        assertNull(vm.waveform.value)
        assertNull(vm.audio.value)
        assertNull(vm.actionError.value, "an absent waveform is not an error")
        assertIs<SegmentDetailUiState.Loaded>(vm.state.value)
    }

    @Test fun onRefreshReFetchesAndTogglesIsRefreshing() = runTest(dispatcher) {
        val repo = repoWith(events = listOf(chunk("e1", 1)))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertEquals(0, vm.revisionValue.value)
        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")

        vm.onRefresh()
        assertTrue(vm.isRefreshing.value, "refresh flag set immediately")
        testScheduler.advanceUntilIdle()

        assertEquals(1, vm.revisionValue.value, "revision bumped so the one-shot flow re-collects")
        assertFalse(vm.isRefreshing.value, "refresh flag cleared after the re-fetch")
    }

    @Test fun renameWritesThroughAndRefreshes() = runTest(dispatcher) {
        val repo = repoWith(events = listOf(chunk("e1", 1)))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.rename("  Studio standup  ")
        testScheduler.advanceUntilIdle()

        assertEquals("s1:1" to "Studio standup", repo.renamed, "trimmed before it reaches the server")
        val loaded = assertIs<SegmentDetailUiState.Loaded>(vm.state.value)
        assertEquals("Studio standup", loaded.summary.title)
        assertNull(vm.actionError.value)
    }

    @Test fun renameRejectsBlankAndOverlongTitlesBeforeTheRoundTrip() = runTest(dispatcher) {
        // The server 400s on both; catching it here avoids a pointless
        // request and an error message for something the client can see.
        val repo = repoWith()
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.rename("   ")
        vm.rename("x".repeat(SegmentDetailViewModel.TITLE_MAX_CHARS + 1))
        testScheduler.advanceUntilIdle()

        assertNull(repo.renamed, "no request issued for an invalid title")
    }

    @Test fun deleteFlipsTheDeletedFlag() = runTest(dispatcher) {
        val repo = repoWith()
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.delete()
        testScheduler.advanceUntilIdle()

        assertEquals("s1:1", repo.deletedId)
        assertTrue(vm.deleted.value, "the host pops back once the server confirms")
    }

    @Test fun deleteWhileStillRecordingSaysSoRatherThanFailingGenerically() = runTest(dispatcher) {
        // The server refuses with 409 while the segment is open, because a
        // delete would race the ingest path. "Try again once it ends" is
        // actionable; "couldn't delete" is not.
        val repo = repoWith()
        repo.deleteResult = Outcome.Failure(ApiError.Http(409))
        val vm = SegmentDetailViewModel(id, repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.delete()
        testScheduler.advanceUntilIdle()

        assertFalse(vm.deleted.value, "nothing was deleted")
        assertEquals("This is still recording. Try again once it ends.", vm.actionError.value)
    }

    /** Captures nameSpeaker/reassignSpeaker calls so the VM tests can assert
     *  the control was emitted with the right ids/scope — without a socket. */
    private class RecordingSpeakerActions : SpeakerActions {
        data class Named(val sessionId: String, val speakerId: String, val name: String)
        data class Reassigned(
            val sessionId: String,
            val fromId: String,
            val toId: String,
            val scope: String,
        )

        val named = mutableListOf<Named>()
        val reassigned = mutableListOf<Reassigned>()

        override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
            named += Named(sessionId, speakerId, name)
        }

        override suspend fun reassignSpeaker(
            sessionId: String,
            fromId: String,
            toId: String,
            scope: String,
        ) {
            reassigned += Reassigned(sessionId, fromId, toId, scope)
        }
    }

    @Test fun renameSpeakerEmitsControlAndUpdatesCacheOptimistically() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("spk-1", null, isWearer = true)
        val actions = RecordingSpeakerActions()
        val vm = SegmentDetailViewModel(id, repoWith(), cache, actions)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.renameSpeaker("spk-1", "Sarah")
        testScheduler.advanceUntilIdle()

        // Optimistic cache update: the label renders immediately.
        assertEquals("Sarah", cache.get("spk-1")?.name)
        assertEquals(true, cache.get("spk-1")?.isWearer, "rename preserves isWearer")
        assertEquals(1, actions.named.size)
        assertEquals("s1", actions.named[0].sessionId, "the segment's session id, not the segment id")
        assertEquals("spk-1", actions.named[0].speakerId)
        assertEquals("Sarah", actions.named[0].name)
    }

    @Test fun reassignSpeakerEmitsControlWithScopeAllAndRefreshes() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("spk-1", "Sarah", isWearer = false)
        cache.upsert("spk-2", "Lee", isWearer = true)
        val actions = RecordingSpeakerActions()
        val vm = SegmentDetailViewModel(id, repoWith(), cache, actions)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertEquals(0, vm.revisionValue.value)

        vm.reassignSpeaker("spk-1", "spk-2")
        testScheduler.advanceUntilIdle()

        assertEquals(1, actions.reassigned.size)
        assertEquals("spk-1", actions.reassigned[0].fromId)
        assertEquals("spk-2", actions.reassigned[0].toId)
        assertEquals("all", actions.reassigned[0].scope, "v1 uses scope=all")
        // Load-bearing: only a successful reassign bumps the revision, so
        // deleting `onSuccess { onRefresh() }` fails this.
        assertEquals(1, vm.revisionValue.value, "labels re-fetched after a reassign")
        assertNull(vm.actionError.value, "no error on success")
    }

    /** A [SpeakerActions] whose calls throw, to exercise the revert path. */
    private class ThrowingSpeakerActions : SpeakerActions {
        override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) =
            error("rename failed")
        override suspend fun reassignSpeaker(
            sessionId: String,
            fromId: String,
            toId: String,
            scope: String,
        ) = error("reassign failed")
    }

    @Test fun renameSpeakerRevertsCacheOnFailure() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("spk-1", "Old", isWearer = false)
        val vm = SegmentDetailViewModel(id, repoWith(), cache, ThrowingSpeakerActions())
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.renameSpeaker("spk-1", "New")
        testScheduler.advanceUntilIdle()

        assertEquals("Old", cache.get("spk-1")?.name, "cache reverted to prior name on failure")
        assertNotNull(vm.actionError.value, "the failure surfaces — the user asked for this")
    }

    @Test fun youConfirmationPromptsOnFirstWearerYouTranscript() = runTest(dispatcher) {
        val cache = SpeakerCache()
        val actions = RecordingSpeakerActions()
        val repo = repoWith(
            events = listOf(chunk("e1", 1, speaker = "you", speakerName = "You", isWearer = true)),
        )
        val vm = SegmentDetailViewModel(id, repo, cache, actions)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        assertEquals(YouConfirmationState.Prompting("you"), vm.youConfirmation.value)

        vm.confirmYou("Kevin")
        testScheduler.advanceUntilIdle()
        assertEquals(YouConfirmationState.Done, vm.youConfirmation.value)
        assertEquals("Kevin", cache.get("you")?.name, "confirmYou renames the wearer in the cache")
        assertEquals(1, actions.named.size, "confirmYou emits a name_speaker control")
        assertEquals("Kevin", actions.named[0].name)
    }

    @Test fun youConfirmationDoesNotPromptWhenAlreadyNamed() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("you", "Kevin", isWearer = true)
        val repo = repoWith(
            events = listOf(chunk("e1", 1, speaker = "you", speakerName = "Kevin", isWearer = true)),
        )
        val vm = SegmentDetailViewModel(id, repo, cache, RecordingSpeakerActions())
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        assertEquals(YouConfirmationState.Idle, vm.youConfirmation.value)
    }

    @Test fun cacheSeededFromLoadedEventsPreservesRealNames() = runTest(dispatcher) {
        val cache = SpeakerCache()
        cache.upsert("sp-1", "Sarah", isWearer = false) // renamed earlier
        // sp-1 is still unnamed on the wire (the server hasn't refreshed) and
        // sp-2 is brand new. Seeding must populate sp-2 without overwriting
        // sp-1's renamed name.
        val repo = repoWith(
            events = listOf(
                chunk("e1", 1, speaker = "sp-1"),
                chunk("e2", 2, speaker = "sp-2"),
            ),
        )
        val vm = SegmentDetailViewModel(id, repo, cache, RecordingSpeakerActions())
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        assertEquals("Sarah", cache.get("sp-1")?.name)
        // sp-2 is seeded (null name) so the reassign picker is non-empty.
        assertNotNull(cache.get("sp-2"))
        assertNull(cache.get("sp-2")?.name)
    }
}
