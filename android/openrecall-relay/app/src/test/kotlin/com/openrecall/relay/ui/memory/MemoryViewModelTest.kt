package com.openrecall.relay.ui.memory

import com.openrecall.relay.data.MemoryApi
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.MemoryRepository
import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.MemoryListResponseDto
import com.openrecall.relay.http.dto.MemorySearchResponseDto
import com.openrecall.relay.http.dto.MemoryStatsResponseDto
import com.openrecall.relay.http.dto.SessionMemoryResponseDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Before
import org.junit.Test
import kotlin.test.assertEquals
import kotlin.test.assertNull
import kotlin.test.assertTrue

/**
 * Pins [MemoryViewModel]'s two modes.
 *
 * Browse (no query) is the default and is a paged, kind-filtered list; search
 * is ranked and unpaged. The tests that matter are the ones about which call
 * gets made: pushing the kind filter to the server is what makes a filter
 * find memories the user hasn't scrolled to.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class MemoryViewModelTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() {
        Dispatchers.setMain(dispatcher)
    }

    @After
    fun tearDown() {
        Dispatchers.resetMain()
    }

    private fun atom(id: String, kind: String = "fact") =
        MemoryAtom(id, "s1", kind, "hello", "2026-07-07", "2026-07-06", 0, "e1", "transcript", "v1", "bge", "v1")

    @Test
    fun `browses on construction without a query`() = runTest(dispatcher) {
        // Before the server grew list mode this screen could not render at
        // all: /memory required a query. The default view is now a browse.
        val api = CountingMemoryApi(listAtoms = listOf(atom("a1")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()

        assertEquals(1, api.listCalls, "browsed once on construction")
        assertEquals(0, api.searchCalls, "no query, no search")
        assertEquals(listOf("a1"), vm.state.value.atoms.map { it.atomId })
    }

    @Test
    fun `stats populate the header and the filter chips`() = runTest(dispatcher) {
        // The chips come from the kinds that actually exist. A hardcoded row
        // would offer filters matching nothing while hiding kinds that do.
        val api = CountingMemoryApi(
            stats = MemoryStatsResponseDto(
                total = 128, added_24h = 6, by_kind = mapOf("fact" to 80, "task" to 41, "scene" to 7),
            ),
        )
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()

        assertEquals(128, vm.state.value.stats.total)
        assertEquals(6, vm.state.value.stats.added24h)
        assertEquals(
            listOf("All", "fact", "task", "scene"),
            vm.state.value.filters,
            "chips are ordered by how common each kind is",
        )
    }

    @Test
    fun `kind filter is pushed to the server in browse mode`() = runTest(dispatcher) {
        val api = CountingMemoryApi(listAtoms = listOf(atom("a1", kind = "task")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()

        vm.onFilterSelected("task")
        advanceUntilIdle()

        assertEquals("task", api.lastKind, "the filter reaches the server, not just the loaded page")
        assertEquals(2, api.listCalls, "the filter re-queries")
    }

    @Test
    fun `search with a valid query returns atoms`() = runTest(dispatcher) {
        val api = CountingMemoryApi(searchAtoms = listOf(atom("a1")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()

        vm.onQueryChanged("hello")
        advanceUntilIdle()

        assertEquals(1, api.searchCalls)
        assertEquals(listOf("a1"), vm.state.value.atoms.map { it.atomId })
        assertEquals("hello", vm.state.value.lastQuery)
        assertNull(vm.state.value.nextCursor, "search is unpaged")
    }

    @Test
    fun `clearing the query returns to the browse list`() = runTest(dispatcher) {
        // Otherwise clearing the box strands the last search results on
        // screen with nothing indicating they are stale.
        val api = CountingMemoryApi(listAtoms = listOf(atom("a1")), searchAtoms = listOf(atom("a2")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()
        vm.onQueryChanged("hello")
        advanceUntilIdle()
        assertEquals(listOf("a2"), vm.state.value.atoms.map { it.atomId })

        vm.onQueryChanged("")
        advanceUntilIdle()

        assertEquals(listOf("a1"), vm.state.value.atoms.map { it.atomId })
        assertEquals("", vm.state.value.lastQuery)
    }

    @Test
    fun `onLoadMore appends the next page and stops at the end`() = runTest(dispatcher) {
        val api = CountingMemoryApi(listAtoms = listOf(atom("a1")), nextCursor = "c1")
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()
        assertEquals("c1", vm.state.value.nextCursor)

        api.listAtoms = listOf(atom("a2"))
        api.nextCursor = null
        vm.onLoadMore()
        advanceUntilIdle()

        assertEquals(listOf("a1", "a2"), vm.state.value.atoms.map { it.atomId })
        assertNull(vm.state.value.nextCursor)

        val callsAtEnd = api.listCalls
        vm.onLoadMore()
        advanceUntilIdle()
        assertEquals(callsAtEnd, api.listCalls, "no cursor, no request")
    }

    @Test
    fun `onLoadMore does nothing in search mode`() = runTest(dispatcher) {
        // Search results arrive whole; a "load more" here would re-run the
        // browse query and append unrelated rows under the hits.
        val api = CountingMemoryApi(searchAtoms = listOf(atom("a1")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()
        vm.onQueryChanged("hello")
        advanceUntilIdle()
        val listCalls = api.listCalls

        vm.onLoadMore()
        advanceUntilIdle()

        assertEquals(listCalls, api.listCalls)
    }

    @Test
    fun `session memory returns session atoms`() = runTest(dispatcher) {
        val api = CountingMemoryApi(sessionAtoms = listOf(atom("a1")))
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()

        vm.loadSession("s1")
        advanceUntilIdle()

        assertEquals(1, vm.state.value.atoms.size)
    }

    @Test
    fun `error from repository shows in state`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(FailingMemoryApi()))
        vm.onQueryChanged("x")
        advanceUntilIdle()
        assertTrue(vm.state.value.errorMessage != null)
    }

    @Test
    fun `onRefresh re-runs the last search even after the query box is cleared`() =
        runTest(dispatcher) {
            val api = CountingMemoryApi(searchAtoms = listOf(atom("a1")))
            val vm = MemoryViewModel(MemoryRepository(api))
            advanceUntilIdle()
            vm.onQueryChanged("hello")
            advanceUntilIdle()
            assertEquals(1, api.searchCalls, "initial search ran once")

            // The box is cleared through the state only — onRefresh must
            // re-run what the list is showing, which is still the search.
            vm.onRefresh()
            advanceUntilIdle()

            assertEquals(2, api.searchCalls, "onRefresh re-ran the last search")
            assertEquals("hello", vm.state.value.lastQuery, "lastQuery preserved")
            assertEquals(1, vm.state.value.atoms.size, "atoms re-populated")
        }

    @Test
    fun `onRefresh re-reads the totals`() = runTest(dispatcher) {
        // Extraction runs in batch, so the counts move without the list
        // changing; a refresh that skipped them would show stale totals.
        val api = CountingMemoryApi()
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()
        assertEquals(1, api.statsCalls)

        vm.onRefresh()
        advanceUntilIdle()

        assertEquals(2, api.statsCalls)
    }

    @Test
    fun `onRefresh re-browses when nothing else has been loaded`() = runTest(dispatcher) {
        val api = CountingMemoryApi()
        val vm = MemoryViewModel(MemoryRepository(api))
        advanceUntilIdle()
        val before = api.listCalls

        vm.onRefresh()
        advanceUntilIdle()

        assertEquals(before + 1, api.listCalls)
        assertEquals(0, api.searchCalls, "no search without a query")
    }
}

private class FailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun list(
        kind: String?,
        sessionId: String?,
        limit: Int,
        cursor: String?,
    ): MemoryListResponseDto = throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun stats(): MemoryStatsResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}

/** Records which endpoint the ViewModel actually reached for, and with what. */
private class CountingMemoryApi(
    var listAtoms: List<MemoryAtom> = emptyList(),
    val searchAtoms: List<MemoryAtom> = emptyList(),
    val sessionAtoms: List<MemoryAtom> = emptyList(),
    var nextCursor: String? = null,
    val stats: MemoryStatsResponseDto = MemoryStatsResponseDto(),
) : MemoryApi("http://test", "t", OkHttpClient()) {
    var searchCalls = 0
    var listCalls = 0
    var statsCalls = 0
    var sessionCalls = 0
    var lastKind: String? = null

    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto {
        searchCalls++
        return MemorySearchResponseDto(
            schema_version = "v1",
            request_id = "r",
            retrieval_trace_id = "t",
            audit_id = "a",
            query = query,
            atoms = searchAtoms.map { it.toDto() },
            returned_count = searchAtoms.size,
        )
    }

    override suspend fun list(
        kind: String?,
        sessionId: String?,
        limit: Int,
        cursor: String?,
    ): MemoryListResponseDto {
        listCalls++
        lastKind = kind
        return MemoryListResponseDto(
            atoms = listAtoms.map { it.toDto() },
            returned_count = listAtoms.size,
            next_cursor = nextCursor,
        )
    }

    override suspend fun stats(): MemoryStatsResponseDto {
        statsCalls++
        return stats
    }

    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto {
        sessionCalls++
        return SessionMemoryResponseDto(
            schema_version = "v1",
            session_id = sessionId,
            atoms = sessionAtoms.map { it.toDto() },
            returned_count = sessionAtoms.size,
        )
    }
}

private fun MemoryAtom.toDto() = com.openrecall.relay.http.dto.MemoryAtomDto(
    schema_version = "v1",
    atom_id = atomId,
    session_id = sessionId,
    kind = kind,
    text = text,
    created_at = createdAt,
    occurred_at = occurredAt,
    start_ms = startMs,
    source_event_id = sourceEventId,
    source_modality = sourceModality,
    extraction_version = extractionVersion,
    embedding_model = embeddingModel,
    extractor_prompt_version = extractorPromptVersion,
)
