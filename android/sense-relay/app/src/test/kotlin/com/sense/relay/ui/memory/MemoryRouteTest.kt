package com.sense.relay.ui.memory

import com.sense.relay.data.MemoryApi
import com.sense.relay.data.MemoryAtom
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

/**
 * Robolectric-free smoke test for the [MemoryRoute] -> [MemoryViewModel] ->
 * [MemoryScreen] chain. The actual Composable rendering is validated
 * manually on a real device (the project's standing decision per the
 * 2026-07-17 project-status memory: Robolectric PR #4736 blocks
 * host-JVM Compose UI tests in this sandbox).
 *
 * This test asserts the ViewModel wiring that the route owns: a real
 * [MemoryViewModel] built on a stub [MemoryRepository] returns the
 * expected [MemoryState] on a search, and the error path puts a
 * non-null [MemoryState.errorMessage] in state. The Route's
 * `viewModelFactory { initializer { ... } }` is the same pattern as
 * ChatRoute / CommandsRoute; the architectural invariant
 * `ui/` must not import http.* types guards it.
 *
 * The test stubs are suffixed with "Route" (vs the identical stubs in
 * [MemoryViewModelTest]) because Kotlin's `private` at file scope
 * means file-private — the helpers cannot be shared between files,
 * even within the same package.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class MemoryRouteTest {

    private val dispatcher = StandardTestDispatcher()

    @Before
    fun setUp() { Dispatchers.setMain(dispatcher) }
    @After
    fun tearDown() { Dispatchers.resetMain() }

    @Test
    fun `ViewModel constructed with a working repo returns atoms on search`() = runTest(dispatcher) {
        val atoms = listOf(
            MemoryAtom("a1", "s1", "fact", "hello", "2026-07-19", 0, "e1", "transcript", "v1", "bge", "v1"),
        )
        val vm = MemoryViewModel(MemoryRepository(RouteStubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("hello")
        vm.search()
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
        assertEquals("a1", vm.state.value.atoms[0].atomId)
    }

    @Test
    fun `error path puts errorMessage in state`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(RouteFailingMemoryApi()))
        vm.onQueryChanged("x")
        vm.search()
        advanceUntilIdle()
        assertTrue(vm.state.value.errorMessage != null)
    }
}

private class RouteStubMemoryApi(
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
            atoms = searchAtoms.map { it.toRouteDto() },
            returned_count = searchAtoms.size,
        )
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        SessionMemoryResponseDto(
            schema_version = "v1",
            session_id = sessionId,
            atoms = sessionAtoms.map { it.toRouteDto() },
            returned_count = sessionAtoms.size,
        )
}

private class RouteFailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}

private fun MemoryAtom.toRouteDto() = com.sense.relay.http.dto.MemoryAtomDto(
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
