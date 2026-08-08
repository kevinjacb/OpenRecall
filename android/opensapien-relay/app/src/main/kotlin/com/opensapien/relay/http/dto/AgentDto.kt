package com.opensapien.relay.http.dto

import kotlinx.serialization.Serializable

/**
 * Server wire DTOs for the cognitive read path.
 *
 * Names and JSON keys mirror the server DTOs in
 * `server/src/opensapien_server/http/routes/dto.py`. Field renames are a
 * bug; ``schema_version`` is the binding forward-compat handle.
 */

@Serializable
data class AgentRequestDto(
    val schema_version: String = "v1",
    val session_id: String? = null,
    val text: String,
    val limit: Int = 10,
)

@Serializable
data class AgentResponseDto(
    val schema_version: String,
    val request_id: String,
    val retrieval_trace_id: String,
    val audit_id: String,
    val outcome: String,             // "return" | "return_with_uncertainty" | "refuse"
    val answer: String? = null,
    val atoms: List<AtomChipDto> = emptyList(),
    val confidence: Double? = null,
    val confidence_band: String? = null,
    val refusal_reason: String? = null,
    val payload: kotlinx.serialization.json.JsonElement? = null,
)

@Serializable
data class AtomChipDto(
    val atom_id: String,
    val session_id: String,
    val kind: String,
    val text: String,
    val created_at: String,
    val start_ms: Int,
    val score: Double,
)

@Serializable
data class MemoryAtomDto(
    val schema_version: String,
    val atom_id: String,
    val session_id: String,
    val kind: String,
    val text: String,
    val created_at: String,
    val start_ms: Int,
    val source_event_id: String,
    val source_modality: String,
    val extraction_version: String,
    val embedding_model: String,
    val extractor_prompt_version: String,
)

@Serializable
data class MemorySearchResponseDto(
    val schema_version: String,
    val request_id: String,
    val retrieval_trace_id: String,
    val audit_id: String,
    val query: String,
    val session_id: String? = null,
    val atoms: List<MemoryAtomDto> = emptyList(),
    val returned_count: Int = 0,
    val top_score: Double = 0.0,
    val retrieval_latency_ms: Int = 0,
)

@Serializable
data class SessionMemoryResponseDto(
    val schema_version: String,
    val session_id: String,
    val atoms: List<MemoryAtomDto> = emptyList(),
    val returned_count: Int = 0,
)

@Serializable
data class ErrorEnvelopeDto(
    val schema_version: String,
    val code: String,
    val message: String,
    val request_id: String? = null,
)
