package com.opensapien.relay.ui.recordings

import com.opensapien.relay.data.FakeSessionRepository
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
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
 * [com.opensapien.relay.core.model.PagedResult] to [RecordingsUiState], appends
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

    @Test fun queryFiltersLoadedSessionsByPreviewAndId() = runTest(dispatcher) {
        // The sessions API has no search parameter, so the Recordings search
        // box filters what has already been paged in. Matching must cover the
        // preview text and the id — the id is what the row headline shows.
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("alpha"), summary("beta")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("p-alpha")
        testScheduler.advanceUntilIdle()
        assertEquals(
            listOf("alpha"),
            assertIs<RecordingsUiState.Loaded>(vm.state.value).items.map { it.id.value },
        )

        vm.onQueryChange("BETA")
        testScheduler.advanceUntilIdle()
        assertEquals(
            listOf("beta"),
            assertIs<RecordingsUiState.Loaded>(vm.state.value).items.map { it.id.value },
        )
    }

    @Test fun queryWithNoMatchesRendersEmptyNotError() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("alpha")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("nothing matches this")
        testScheduler.advanceUntilIdle()

        assertIs<RecordingsUiState.Empty>(vm.state.value)
    }

    @Test fun blankQueryShowsEverything() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a"), summary("b")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        vm.onQueryChange("  ")
        testScheduler.advanceUntilIdle()

        assertEquals(
            listOf("a", "b"),
            assertIs<RecordingsUiState.Loaded>(vm.state.value).items.map { it.id.value },
        )
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

    @Test fun onRefreshResetsToPageOneAndTogglesIsRefreshing() = runTest(dispatcher) {
        // After loading two pages (a,b then c,d), a pull-to-refresh resets to
        // page 1: the accumulated list is cleared and page 1 (a,b) is re-served.
        // The Fake re-serves from its queuedOriginal snapshot, so no re-queue
        // is needed.
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a"), summary("b")), nextCursor = "c1")
        repo.queue(listOf(summary("c"), summary("d")), nextCursor = null)
        val vm = RecordingsViewModel(repo)
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()
        vm.onLoadMore() // load page 2
        testScheduler.advanceUntilIdle()
        assertEquals(4, (vm.state.value as RecordingsUiState.Loaded).items.size)

        assertFalse(vm.isRefreshing.value, "not refreshing before the gesture")
        vm.onRefresh()
        // While in flight, the flag is set.
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
        // second onRefresh is a no-op (the flag guard), so the repo's
        // refreshSessions is called at most once.
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a")), nextCursor = "c1")
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
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a"), summary("b")), nextCursor = "c1")
        val vm = RecordingsViewModel(repoWithLoadError(repo, com.opensapien.relay.core.model.ApiError.Unreachable("timeout")))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(listOf("a", "b"), loaded.items.map { it.id.value }, "items stay")
        val err = assertIs<com.opensapien.relay.core.model.ApiError.Unreachable>(loaded.loadError)
        assertEquals("timeout", err.reason)
    }

    @Test fun successfulRetryClearsTheInlineLoadError() = runTest(dispatcher) {
        val repo = FakeSessionRepository()
        repo.queue(listOf(summary("a")), nextCursor = "c1")
        val errors = kotlinx.coroutines.flow.MutableStateFlow<com.opensapien.relay.core.model.ApiError?>(null)
        val vm = RecordingsViewModel(repoWithLoadError(repo, errors))
        backgroundScope.launch { vm.state.toList(mutableListOf()) }
        testScheduler.advanceUntilIdle()

        errors.value = com.opensapien.relay.core.model.ApiError.Unreachable("down")
        testScheduler.advanceUntilIdle()
        assertIs<com.opensapien.relay.core.model.ApiError.Unreachable>(
            (vm.state.value as RecordingsUiState.Loaded).loadError,
        )

        errors.value = null // retry cleared it
        testScheduler.advanceUntilIdle()
        val loaded = assertIs<RecordingsUiState.Loaded>(vm.state.value)
        assertEquals(null, loaded.loadError, "a successful retry clears the inline error")
    }
}

/**
 * Wrap a [FakeSessionRepository] so its [SessionRepository.observeLoadErrors]
 * returns a controllable flow (the Fake inherits the interface default
 * `flowOf(null)`, which fires `combine` once but can't be toggled). Delegates
 * everything else to the Fake.
 */
private fun repoWithLoadError(
    delegate: FakeSessionRepository,
    errors: kotlinx.coroutines.flow.Flow<com.opensapien.relay.core.model.ApiError?>,
): com.opensapien.relay.data.SessionRepository = object : com.opensapien.relay.data.SessionRepository {
    override fun observeSessions() = delegate.observeSessions()
    override suspend fun loadMoreSessions() = delegate.loadMoreSessions()
    override fun observeSession(id: SessionId) = delegate.observeSession(id)
    override fun observeSessionEvents(id: SessionId) = delegate.observeSessionEvents(id)
    override fun observeLoadErrors(): kotlinx.coroutines.flow.Flow<com.opensapien.relay.core.model.ApiError?> = errors
}

private fun repoWithLoadError(
    delegate: FakeSessionRepository,
    error: com.opensapien.relay.core.model.ApiError,
): com.opensapien.relay.data.SessionRepository =
    repoWithLoadError(delegate, kotlinx.coroutines.flow.flowOf(error))
