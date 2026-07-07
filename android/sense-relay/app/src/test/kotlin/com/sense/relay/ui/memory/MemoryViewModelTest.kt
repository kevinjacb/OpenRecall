package com.sense.relay.ui.memory

import com.sense.relay.data.MemoryApi
import com.sense.relay.data.MemoryAtom
import com.sense.relay.data.MemoryOutcome
import com.sense.relay.data.MemoryRepository
import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.MemorySearchResponseDto
import com.sense.relay.http.dto.SessionMemoryResponseDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.resetMain
import kotlinx.coroutines.test.runTest
import kotlinx.coroutines.test.setMain
import okhttp3.OkHttpClient
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

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

private fun MemoryAtom.toDto() = com.sense.relay.http.dto.MemoryAtomDto(
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
