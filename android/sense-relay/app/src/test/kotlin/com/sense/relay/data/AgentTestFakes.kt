package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.AgentResponseDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.OkHttpClient

/** Test fixtures: hand-built AgentResponseDto + failing/fake AgentApi. */

data class AgentResponseSample(
    val outcome: String,
    val answer: String? = null,
    val atoms: List<AtomChipSample> = emptyList(),
    val confidence: Double? = null,
    val confidenceBand: String? = null,
    val refusalReason: String? = null,
    val requestId: String = "req-1",
    val retrievalTraceId: String = "trace-1",
    val auditId: String = "audit-1",
) {
    fun toDto(): AgentResponseDto = AgentResponseDto(
        schema_version = "v1",
        request_id = requestId,
        retrieval_trace_id = retrievalTraceId,
        audit_id = auditId,
        outcome = outcome,
        answer = answer,
        atoms = atoms.map { it.toDto() },
        confidence = confidence,
        confidence_band = confidenceBand,
        refusal_reason = refusalReason,
    )
}

data class AtomChipSample(
    val atomId: String,
    val text: String,
    val sessionId: String = "s1",
    val kind: String = "fact",
    val createdAt: String = "2026-07-07T07:00:00+00:00",
    val startMs: Int = 0,
    val score: Double = 0.9,
) {
    fun toDto() = com.sense.relay.http.dto.AtomChipDto(
        atom_id = atomId,
        session_id = sessionId,
        kind = kind,
        text = text,
        created_at = createdAt,
        start_ms = startMs,
        score = score,
    )
}

class FakeAgentApi(private val response: AgentResponseSample) : AgentApi(
    baseUrl = "http://test",
    token = "t",
    client = OkHttpClient(),
) {
    override suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int,
    ): AgentResponseDto = response.toDto()
}

class FailingAgentApi : AgentApi(
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
