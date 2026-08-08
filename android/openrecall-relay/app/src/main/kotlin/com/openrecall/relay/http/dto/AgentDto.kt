package com.openrecall.relay.http.dto

import kotlinx.serialization.Serializable

/**
 * Server wire DTOs for the cognitive read path.
 *
 * Names and JSON keys mirror the server DTOs in
 * `server/src/openrecall_server/http/routes/dto.py`. Field renames are a
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
    /**
     * Conversation time — when this was *said*, not when the server extracted
     * it. Extraction runs in batch after the fact, so `created_at` would
     * report last night's conversation as "added today" and give every atom
     * in it one identical timestamp. This is the field the list orders and
     * groups on. Null only for atoms written before the field existed.
     */
    val occurred_at: String? = null,
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

/**
 * `GET /memory` in **list mode** (no `q`) — a page of the browsable
 * collection, ordered by `occurred_at` descending.
 *
 * Deliberately a different shape from [MemorySearchResponseDto]: search
 * returns ranked hits with a relevance score and a retrieval trace, listing
 * returns a page with a cursor. Collapsing them would mean a `top_score`
 * that is meaningless half the time.
 */
@Serializable
data class MemoryListResponseDto(
    val schema_version: String = "v1",
    val atoms: List<MemoryAtomDto> = emptyList(),
    val returned_count: Int = 0,
    val next_cursor: String? = null,
)

/**
 * `GET /memory/stats` — the Memories header ("128 memories · 6 today").
 *
 * [by_kind] is also how the client discovers the kind vocabulary: the
 * extractor emits free-form kinds, so the filter chips are built from what
 * actually exists rather than from a hardcoded list that silently hides
 * everything else.
 */
@Serializable
data class MemoryStatsResponseDto(
    val schema_version: String = "v1",
    val total: Int = 0,
    val added_24h: Int = 0,
    val by_kind: Map<String, Int> = emptyMap(),
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
