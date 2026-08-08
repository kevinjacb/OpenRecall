package com.openrecall.relay.ui.recordings

import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.model.PagedResult
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.data.FakeSegmentRepository
import com.openrecall.relay.data.SegmentRepository
import com.openrecall.relay.data.testSegment
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentDetails
import com.openrecall.relay.domain.model.SegmentId
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.Flow
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
import kotlin.test.assertIs
import kotlin.test.assertFalse
import kotlin.test.assertTrue

/**
 * Pins [RecordingsViewModel]'s paging and search behaviour against the
 * scripted [FakeSegmentRepository]: it kicks the first page on construction,
 * maps [PagedResult] to [RecordingsUiState], appends on
 * [RecordingsViewModel.onLoadMore], and stops when the cursor runs out.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class RecordingsViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @BeforeTest fun setUp() = Dispatchers.setMain(dispatcher)
    @AfterTest fun tearDown() = Dispatchers.resetMain()

    private fun segment(id: String) = testSegment(id, startedAt = Instant.EPOCH)

    @Test fun kicksFirstPageAndMapsToLoaded() = runTest(dispatcher) {
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a"), segment("b")), nextCursor = "c1")
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(listOf("a", "b"), loaded.items.map { it.id.value })
        assertTrue(loaded.canLoadMore, "cursor c1 is non-null")
        assertFalse(loaded.searching, "no query yet")
    }

    @Test fun queryIsPushedToTheServer() = runTest(dispatcher) {
        // The old screen filtered the pages already loaded, which silently
        // missed every recording the user hadn't scrolled to. The query must
        // reach the repository so the server scans the transcripts.
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("standup")
        testScheduler.advanceUntilIdle()

        assertEquals("standup", repo.lastQuery)
        assertTrue(assertIs<RecordingsUiState.Loaded>(vm.state.value).searching)
    }

    @Test fun queryIsDebouncedIntoASingleFetch() = runTest(dispatcher) {
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("s")
        vm.onQueryChange("st")
        vm.onQueryChange("sta")
        testScheduler.advanceUntilIdle()

        // Only the final keystroke survives the debounce.
        assertEquals("sta", repo.lastQuery)
    }

    @Test fun clearingTheQueryReturnsToBrowseMode() = runTest(dispatcher) {
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a"), segment("b")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("alpha")
        testScheduler.advanceUntilIdle()
        vm.onQueryChange("  ")
        testScheduler.advanceUntilIdle()

        assertEquals("", repo.lastQuery, "a blank query clears the server-side search")
        assertFalse(assertIs<RecordingsUiState.Loaded>(vm.state.value).searching)
    }

    @Test fun onLoadMoreAppendsNextPage() = runTest(dispatcher) {
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = "c1")
        repo.queue(listOf(segment("b")), nextCursor = null)
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
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = null)
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
        val repo = FakeSegmentRepository()
        repo.queue(emptyList(), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        assertIs<RecordingsUiState.Empty>(vm.state.value)
    }

    @Test fun onRefreshResetsToPageOneAndTogglesIsRefreshing() = runTest(dispatcher) {
        // After loading two pages (a,b then c,d), a pull-to-refresh resets to
        // page 1: the accumulated list is cleared and page 1 (a,b) is
        // re-served from the Fake's snapshot.
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a"), segment("b")), nextCursor = "c1")
        repo.queue(listOf(segment("c"), segment("d")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        vm.onLoadMore() // load page 2
        testScheduler.advanceUntilIdle()
        assertEquals(4, (vm.state.value as RecordingsUiState.Loaded).items.size)

        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")
        vm.onRefresh()
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(
            listOf("a", "b"),
            loaded.items.map { it.id.value },
            "refresh reset the list to page 1",
        )
        assertTrue(loaded.canLoadMore, "page-1 cursor c1 is non-null again")
        assertFalse(vm.isRefreshing.value, "isRefreshing cleared after the refresh completes")
    }

    @Test fun onRefreshIsNoOpWhileALoadIsInFlight() = runTest(dispatcher) {
        // A refresh during an in-flight loadMore must not double-fetch: the
        // second call is a no-op (the flag guard), so the repo's refresh is
        // not invoked.
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = "c1")
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        val refreshesBefore = repo.refreshCount

        vm.onLoadMore() // in flight, not yet advanced
        vm.onRefresh()  // must be skipped
        testScheduler.advanceUntilIdle()

        assertEquals(refreshesBefore, repo.refreshCount, "refresh skipped while a load is in flight")
    }

    @Test fun pagingErrorSurfacesAsInlineLoadErrorKeepingTheList() = runTest(dispatcher) {
        // A paging failure (items already loaded) must NOT replace the list
        // with a full-screen error: the items stay and the error is a side-
        // channel the VM folds into Loaded.loadError (inline "Retry" row).
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a"), segment("b")), nextCursor = "c1")
        val vm = RecordingsViewModel(
            repoWithLoadError(repo, flowOf(ApiError.Unreachable("timeout"))),
        )
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(listOf("a", "b"), loaded.items.map { it.id.value }, "items stay")
        val err = assertIs<ApiError.Unreachable>(loaded.loadError)
        assertEquals("timeout", err.reason)
    }

    @Test fun successfulRetryClearsTheInlineLoadError() = runTest(dispatcher) {
        val repo = FakeSegmentRepository()
        repo.queue(listOf(segment("a")), nextCursor = "c1")
        val errors = MutableStateFlow<ApiError?>(null)
        val vm = RecordingsViewModel(repoWithLoadError(repo, errors))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        errors.value = ApiError.Unreachable("down")
        testScheduler.advanceUntilIdle()
        assertIs<ApiError.Unreachable>((vm.state.value as RecordingsUiState.Loaded).loadError)

        errors.value = null // retry cleared it
        testScheduler.advanceUntilIdle()
        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(null, loaded.loadError, "a successful retry clears the inline error")
    }
}

/**
 * Wrap a [FakeSegmentRepository] so its `observeLoadErrors` returns a
 * controllable flow. Delegates everything else to the Fake.
 */
private fun repoWithLoadError(
    delegate: FakeSegmentRepository,
    errors: Flow<ApiError?>,
): SegmentRepository = object : SegmentRepository {
    override fun observeSegments(): Flow<PagedResult<Segment>> = delegate.observeSegments()
    override fun observeLoadErrors(): Flow<ApiError?> = errors
    override suspend fun loadMore() = delegate.loadMore()
    override suspend fun refresh(silent: Boolean) = delegate.refresh(silent)
    override suspend fun setQuery(query: String) = delegate.setQuery(query)
    override fun observeSegment(id: SegmentId): Flow<Outcome<SegmentDetails>> =
        delegate.observeSegment(id)
    override suspend fun rename(id: SegmentId, title: String) = delegate.rename(id, title)
    override suspend fun delete(id: SegmentId) = delegate.delete(id)
    override suspend fun waveform(id: SegmentId) = delegate.waveform(id)
    override suspend fun memories(id: SegmentId) = delegate.memories(id)
    override suspend fun audioSource(id: SegmentId) = delegate.audioSource(id)
}
