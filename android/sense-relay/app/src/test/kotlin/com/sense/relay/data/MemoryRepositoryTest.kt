package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.MemorySearchResponseDto
import com.sense.relay.http.dto.SessionMemoryResponseDto
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Unit tests for [MemoryRepository]. Mirrors [CommandRepositoryTest]:
 * stubs the wire layer (a [MemoryApi] subclass) and asserts the
 * domain-level [MemoryOutcome] shape.
 *
 * These tests do NOT exercise [RepositoryModule.repos.memoryRepository]
 * (that path is the integration-test surface; the wiring itself is
 * visually confirmed by the RepositoryModule). The repository's
 * behavior — DTO→domain mapping and HttpApiError→MemoryOutcome.Error
 * — is what the Android UI depends on.
 */
class MemoryRepositoryTest {

    @Test
    fun `search maps DTOs to domain MemoryAtoms`() = runTest {
        val repo = MemoryRepository(StubMemoryApi(
            searchDto = MemorySearchResponseDto(
                schema_version = "v1",
                request_id = "r1",
                retrieval_trace_id = "t1",
                audit_id = "a1",
                query = "hello",
                atoms = listOf(
                    com.sense.relay.http.dto.MemoryAtomDto(
                        schema_version = "v1",
                        atom_id = "a1",
                        session_id = "s1",
                        kind = "fact",
                        text = "hello",
                        created_at = "2026-07-19T00:00:00Z",
                        start_ms = 0,
                        source_event_id = "e1",
                        source_modality = "transcript",
                        extraction_version = "v1",
                        embedding_model = "bge",
                        extractor_prompt_version = "v1",
                    ),
                ),
                returned_count = 1,
            ),
        ))
        val result = repo.search("hello", null, 10) as MemoryOutcome.Success
        assertEquals(1, result.atoms.size)
        assertEquals("a1", result.atoms[0].atomId)
        assertEquals("hello", result.atoms[0].text)
        assertEquals("s1", result.atoms[0].sessionId)
    }

    @Test
    fun `sessionAtoms maps DTOs to domain MemoryAtoms`() = runTest {
        val repo = MemoryRepository(StubMemoryApi(
            sessionDto = SessionMemoryResponseDto(
                schema_version = "v1",
                session_id = "s1",
                atoms = emptyList(),
                returned_count = 0,
            ),
        ))
        val result = repo.sessionAtoms("s1") as MemoryOutcome.Success
        assertEquals(0, result.atoms.size)
    }

    @Test
    fun `search on 500 returns Error with code INTERNAL_ERROR`() = runTest {
        val repo = MemoryRepository(FailingMemoryApi())
        val r = repo.search("x", null, 10)
        assertTrue("expected MemoryOutcome.Error, got $r", r is MemoryOutcome.Error)
        assertEquals(ErrorCode.INTERNAL_ERROR, (r as MemoryOutcome.Error).code)
    }
}

private class StubMemoryApi(
    val searchDto: MemorySearchResponseDto? = null,
    val sessionDto: SessionMemoryResponseDto? = null,
) : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        searchDto ?: MemorySearchResponseDto(
            schema_version = "v1",
            request_id = "r",
            retrieval_trace_id = "t",
            audit_id = "a",
            query = query,
            atoms = emptyList(),
            returned_count = 0,
        )
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        sessionDto ?: SessionMemoryResponseDto(
            schema_version = "v1",
            session_id = sessionId,
            atoms = emptyList(),
            returned_count = 0,
        )
}

private class FailingMemoryApi : MemoryApi("http://test", "t", OkHttpClient()) {
    override suspend fun search(query: String, sessionId: String?, limit: Int): MemorySearchResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
    override suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        throw HttpApiError(ErrorCode.INTERNAL_ERROR, 500, "boom")
}
