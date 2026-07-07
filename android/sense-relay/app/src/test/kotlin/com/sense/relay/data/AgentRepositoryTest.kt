package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.AgentResponseDto
import com.sense.relay.http.dto.AtomChipDto
import kotlinx.coroutines.test.runTest
import okhttp3.OkHttpClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class AgentRepositoryTest {

    @Test
    fun `maps return outcome to Answer with chips`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = sampleResponse("return", "x", listOf(AtomChipSample("a1", "hello")), 0.9, "high"),
        ))
        val out = repo.ask("s1", "what?")
        assertTrue(out is AgentOutcome.Answer)
        val ans = out as AgentOutcome.Answer
        assertEquals("x", ans.text)
        assertEquals(1, ans.atoms.size)
        assertEquals(AgentOutcomeKind.RETURN, ans.outcome)
        assertEquals("high", ans.confidenceBand)
    }

    @Test
    fun `maps return_with_uncertainty to Answer with correct kind`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = sampleResponse("return_with_uncertainty", "x", emptyList(), 0.7, "medium"),
        ))
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Answer)
        assertEquals(AgentOutcomeKind.RETURN_WITH_UNCERTAINTY, (out as AgentOutcome.Answer).outcome)
    }

    @Test
    fun `maps refuse outcome to Refuse with reason`() = runTest {
        val repo = AgentRepository(FakeAgentApi(
            response = sampleResponse("refuse", refusalReason = "no_supporting_memory"),
        ))
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Refuse)
        assertEquals("no_supporting_memory", (out as AgentOutcome.Refuse).reason)
    }

    @Test
    fun `maps HttpApiError to Error outcome`() = runTest {
        val repo = AgentRepository(FailingAgentApi())
        val out = repo.ask(null, "what?")
        assertTrue(out is AgentOutcome.Error)
        assertEquals(ErrorCode.INTERNAL_ERROR, (out as AgentOutcome.Error).code)
    }
}

private fun sampleResponse(
    outcome: String,
    answer: String? = null,
    atoms: List<AtomChipSample> = emptyList(),
    confidence: Double? = null,
    confidenceBand: String? = null,
    refusalReason: String? = null,
): AgentResponseDto = AgentResponseDto(
    schema_version = "v1",
    request_id = "req-1",
    retrieval_trace_id = "trace-1",
    audit_id = "audit-1",
    outcome = outcome,
    answer = answer,
    atoms = atoms.map { it.toDto() },
    confidence = confidence,
    confidence_band = confidenceBand,
    refusal_reason = refusalReason,
)

private data class AtomChipSample(
    val atomId: String,
    val text: String,
    val sessionId: String = "s1",
    val kind: String = "fact",
    val createdAt: String = "2026-07-07T07:00:00+00:00",
    val startMs: Int = 0,
    val score: Double = 0.9,
) {
    fun toDto() = AtomChipDto(
        atom_id = atomId,
        session_id = sessionId,
        kind = kind,
        text = text,
        created_at = createdAt,
        start_ms = startMs,
        score = score,
    )
}

private class FakeAgentApi(private val response: AgentResponseDto) : AgentApi(
    baseUrl = "http://test",
    token = "t",
    client = OkHttpClient(),
) {
    override suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int,
    ): AgentResponseDto = response
}

private class FailingAgentApi : AgentApi(
    baseUrl = "http://test",
    token = "t",
    client = OkHttpClient(),
) {
    override suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int,
    ): AgentResponseDto = throw HttpApiError(
        code = ErrorCode.INTERNAL_ERROR,
        httpStatus = 500,
        message = "boom",
    )
}
