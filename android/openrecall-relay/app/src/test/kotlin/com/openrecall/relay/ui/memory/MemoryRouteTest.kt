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
 * expected [MemoryState], and the error path puts a non-null
 * [MemoryState.errorMessage] in state. The architectural invariant
 * `ui/` must not import http.* types guards the boundary itself.
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

    private fun atom(id: String, kind: String, text: String) =
        MemoryAtom(id, "s1", kind, text, "2026-08-08", "2026-08-07", 0, "e1", "transcript", "v1", "bge", "v1")

    @Test
    fun `ViewModel constructed with a working repo returns atoms on search`() = runTest(dispatcher) {
        val atoms = listOf(atom("a1", "fact", "hello"))
        val vm = MemoryViewModel(MemoryRepository(RouteStubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("hello")
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
        assertEquals("a1", vm.state.value.atoms[0].atomId)
    }

    @Test
    fun `kind filter narrows search hits locally without refetching`() = runTest(dispatcher) {
        // The search endpoint has no kind parameter, so while a query is
        // active the filter narrows what came back. `atoms` must stay intact
        // so clearing the filter restores the full result set.
        val atoms = listOf(
            atom("a1", "task", "send the numbers"),
            atom("a2", "person", "Priya owns the timeline"),
        )
        val vm = MemoryViewModel(MemoryRepository(RouteStubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("x")
        advanceUntilIdle()
        assertEquals(2, vm.state.value.visibleAtoms.size)

        vm.onFilterSelected("task")
        assertEquals(listOf("a1"), vm.state.value.visibleAtoms.map { it.atomId })
        assertEquals("the unfiltered result set is retained", 2, vm.state.value.atoms.size)
        assertTrue("a filter over search hits is local", vm.state.value.filterIsLocal)

        vm.onFilterSelected(MemoryState.ALL_FILTER)
        assertEquals(2, vm.state.value.visibleAtoms.size)
    }

    @Test
    fun `filter matching ignores case so extractor casing does not hide memories`() =
        runTest(dispatcher) {
            val atoms = listOf(atom("a1", "task", "lowercase kind"))
            val vm = MemoryViewModel(MemoryRepository(RouteStubMemoryApi(searchAtoms = atoms)))
            vm.onQueryChanged("x")
            advanceUntilIdle()

            vm.onFilterSelected("Task")
            assertEquals(1, vm.state.value.visibleAtoms.size)
        }

    @Test
    fun `typing searches without an explicit submit`() = runTest(dispatcher) {
        val atoms = listOf(atom("a1", "task", "hello"))
        val vm = MemoryViewModel(MemoryRepository(RouteStubMemoryApi(searchAtoms = atoms)))
        vm.onQueryChanged("hello")
        advanceUntilIdle()
        assertEquals(1, vm.state.value.atoms.size)
    }

    @Test
    fun `error path puts errorMessage in state`() = runTest(dispatcher) {
        val vm = MemoryViewModel(MemoryRepository(RouteFailingMemoryApi()))
        vm.onQueryChanged("x")
        advanceUntilIdle()
        assertTrue(vm.state.value.errorMessage != null)
    }
}

private class RouteStubMemoryApi(
    val listAtoms: List<MemoryAtom> = emptyList(),
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

    override suspend fun list(
        kind: String?,
        sessionId: String?,
        limit: Int,
        cursor: String?,
    ): MemoryListResponseDto = MemoryListResponseDto(
        atoms = listAtoms.map { it.toRouteDto() },
        returned_count = listAtoms.size,
    )

    override suspend fun stats(): MemoryStatsResponseDto = MemoryStatsResponseDto()

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

private fun MemoryAtom.toRouteDto() = com.openrecall.relay.http.dto.MemoryAtomDto(
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
