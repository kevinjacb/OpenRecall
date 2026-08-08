package com.openrecall.relay.ui.memory

import com.openrecall.relay.data.MemoryApi
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.MemoryOutcome
import com.openrecall.relay.data.MemoryRepository
import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.MemorySearchResponseDto
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
import kotlin.test.assertTrue

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

    @Test
    fun `search with empty query is rejected`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(StubMemoryApi(emptyList())))
        vm.onQueryChanged("")
        vm.search()
        advanceUntilIdle()
        assertEquals(0, vm.state.value.atoms.size)
    }

    @Test
    fun `search with valid query returns atoms`() = runTest(dispatcher) {
        val atoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-07", 0, "e1", "transcript", "v1", "bge", "v1"),
        )
        val vm = MemoryViewModel(MemoryRepository(StubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("hello")
        vm.search()
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
        assertEquals("a1", vm.state.value.atoms[0].atomId)
    }

    @Test
    fun `session memory returns session atoms`() = runTest(dispatcher) {
        val atoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-07", 0, "e1", "transcript", "v1", "bge", "v1"),
        )
        val vm = MemoryViewModel(MemoryRepository(StubMemoryApi(sessionAtoms = atoms)))
        vm.loadSession("s1")
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
    }

    @Test
    fun `error from repository shows in state`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(FailingMemoryApi()))
        vm.onQueryChanged("x")
        vm.search()
        advanceUntilIdle()
        assertTrue(vm.state.value.errorMessage != null)
    }

    @Test
    fun `last query is recorded for replay`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(StubMemoryApi(searchAtoms = emptyList())))
        vm.onQueryChanged("hello")
        vm.search()
        advanceUntilIdle()
        assertEquals("hello", vm.state.value.lastQuery)
    }

    @Test
    fun `onRefresh re-runs the last search even after the query box is cleared`() = runTest(dispatcher) {
        val api = CountingMemoryApi(searchAtoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-07", 0, "e1", "transcript", "v1", "bge", "v1"),
        ))
        val vm = MemoryViewModel(MemoryRepository(api))
        vm.onQueryChanged("hello")
        vm.search()
        advanceUntilIdle()
        assertEquals(1, api.searchCalls, "initial search ran once")

        // Clear the query box; lastQuery is still "hello". A pull-to-refresh
        // must re-run the last search (not be a no-op on the empty box).
        vm.onQueryChanged("")
        vm.onRefresh()
        advanceUntilIdle()
        assertEquals(2, api.searchCalls, "onRefresh re-ran the last search")
        assertEquals("hello", vm.state.value.lastQuery, "lastQuery preserved")
        assertEquals(1, vm.state.value.atoms.size, "atoms re-populated")
    }

    @Test
    fun `onRefresh re-loads the last session when no search was run`() = runTest(dispatcher) {
        val api = CountingMemoryApi(sessionAtoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-07", 0, "e1", "transcript", "v1", "bge", "v1"),
        ))
        val vm = MemoryViewModel(MemoryRepository(api))
        vm.loadSession("s1")
        advanceUntilIdle()
        assertEquals(1, api.sessionCalls, "initial session load ran once")

        vm.onRefresh()
        advanceUntilIdle()
        assertEquals(2, api.sessionCalls, "onRefresh re-loaded the last session")
    }

    @Test
    fun `onRefresh is a no-op when nothing has been loaded yet`() = runTest(dispatcher) {
        val api = CountingMemoryApi()
        val vm = MemoryViewModel(MemoryRepository(api))
        vm.onRefresh()
        advanceUntilIdle()
        assertEquals(0, api.searchCalls, "no search without a last query")
        assertEquals(0, api.sessionCalls, "no session load without a last session")
    }
}

private class StubMemoryApi(
    val searchAtoms: List<MemoryAtom> = emptyList(),
    val sessionAtoms: List<MemoryAtom> = emptyList(),
) : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        MemorySearchResponseDto(
            schema_version = "v1",
            request_id = "r",
            retrieval_trace_id = "t",
            audit_id = "a",
            query = query,
            atoms = searchAtoms.map { it.toDto() },
            returned_count = searchAtoms.size,
        )

    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        SessionMemoryResponseDto(
            schema_version = "v1",
            session_id = sessionId,
            atoms = sessionAtoms.map { it.toDto() },
            returned_count = sessionAtoms.size,
        )
}

private class FailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}

private class CountingMemoryApi(
    val searchAtoms: List<MemoryAtom> = emptyList(),
    val sessionAtoms: List<MemoryAtom> = emptyList(),
) : MemoryApi("http://test", "t", OkHttpClient()) {
    var searchCalls = 0
    var sessionCalls = 0
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
    start_ms = startMs,
    source_event_id = sourceEventId,
    source_modality = sourceModality,
    extraction_version = extractionVersion,
    embedding_model = embeddingModel,
    extractor_prompt_version = extractorPromptVersion,
)
