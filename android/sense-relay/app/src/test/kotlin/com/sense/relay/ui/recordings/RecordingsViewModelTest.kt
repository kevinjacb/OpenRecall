package com.sense.relay.ui.recordings

import com.sense.relay.data.FakeSessionRepository
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
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
import kotlin.test.assertIs
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Pins [RecordingsViewModel]'s paging behavior against the real
 * [FakeSessionRepository]: it kicks the first page on construction, maps
 * [com.sense.relay.core.model.PagedResult] to [RecordingsUiState], appends
 * on [RecordingsViewModel.onLoadMore], and stops when the cursor runs out.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class RecordingsViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)
    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private fun summary(id: String) = SessionSummary(
        id = SessionId(id),
        startedAt = Instant.EPOCH,
        endedAt = null,
        durationMs = 1000,
        transcriptCount = 1,
        preview = "p-$id",
    )

    @Test fun kicksFirstPageAndMapsToLoaded() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a"), summary("b")), nextCursor = "c1")
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(listOf("a", "b"), loaded.items.map { it.id.value })
        assertTrue(loaded.canLoadMore, "cursor c1 is non-null")
    }

    @Test fun onLoadMoreAppendsNextPage() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a")), nextCursor = "c1")
        repo.queue(listOf(summary("b")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertEquals(1, (vm.state.value as RecordingsUiState.Loaded).items.size)

        vm.onLoadMore()
        testScheduler.advanceUntilIdle()
        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(listOf("a", "b"), loaded.items.map { it.id.value })
        assertFalse(loaded.canLoadMore, "cursor is now null")
    }

    @Test fun onLoadMoreIsNoOpWhenExhausted() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        val before = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertFalse(before.canLoadMore)

        vm.onLoadMore() // must not fetch (no more queued pages, cursor null)
        testScheduler.advanceUntilIdle()
        val after = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(before.items.map { it.id.value }, after.items.map { it.id.value })
    }

    @Test fun emptyTerminalPageMapsToEmpty() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(emptyList(), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertIs<RecordingsUiState.Empty>(vm.state.value)
    }
}
