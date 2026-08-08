package com.opensapien.relay.data

import com.opensapien.relay.http.ErrorCode
import com.opensapien.relay.http.HttpApiError

/**
 * Domain layer for the memory endpoints. Maps [HttpApiError] to a
 * sealed [MemoryOutcome] so the VM sees only domain types.
 *
 * [apiProvider] is a suspend factory that returns the current
 * [MemoryApi] (i.e. with the live (baseUrl, token, client) resolved
 * from the shared [com.opensapien.relay.http.OpenSapienHttpClient] provider).
 * The provider is called on every search/sessionAtoms, so a
 * re-provision in Settings takes effect on the next /memory request
 * without rebuilding the repository. This mirrors [CommandRepository].
 */
open class MemoryRepository(private val apiProvider: suspend () -> MemoryApi) {

    /**
     * Convenience secondary constructor for unit tests that pass a
     * single stub [MemoryApi]. Mirrors [CommandRepository].
     */
    constructor(api: MemoryApi) : this(apiProvider = { api })

    suspend fun search(
        query: String,
        sessionId: String? = null,
        limit: Int = 10,
    ): MemoryOutcome {
        return try {
            val dto = apiProvider().search(query, sessionId, limit)
            MemoryOutcome.Success(
                atoms = dto.atoms.map { it.toDomain() },
                query = dto.query,
                retrievalTraceId = dto.retrieval_trace_id,
            )
        } catch (e: HttpApiError) {
            MemoryOutcome.Error(e.code, e.message)
        }
    }

    suspend fun sessionAtoms(sessionId: String): MemoryOutcome {
        return try {
            val dto = apiProvider().sessionAtoms(sessionId)
            MemoryOutcome.Success(
                atoms = dto.atoms.map { it.toDomain() },
                query = "",
                retrievalTraceId = "",
            )
        } catch (e: HttpApiError) {
            MemoryOutcome.Error(e.code, e.message)
        }
    }
}

data class MemoryAtom(
    val atomId: String,
    val sessionId: String,
    val kind: String,
    val text: String,
    val createdAt: String,
    val startMs: Int,
    val sourceEventId: String,
    val sourceModality: String,
    val extractionVersion: String,
    val embeddingModel: String,
    val extractorPromptVersion: String,
)

sealed class MemoryOutcome {
    data class Success(
        val atoms: List<MemoryAtom>,
        val query: String,
        val retrievalTraceId: String,
    ) : MemoryOutcome()
    data class Error(val code: ErrorCode, val message: String) : MemoryOutcome()
}

private fun com.opensapien.relay.http.dto.MemoryAtomDto.toDomain() = MemoryAtom(
    atomId = atom_id,
    sessionId = session_id,
    kind = kind,
    text = text,
    createdAt = created_at,
    startMs = start_ms,
    sourceEventId = source_event_id,
    sourceModality = source_modality,
    extractionVersion = extraction_version,
    embeddingModel = embedding_model,
    extractorPromptVersion = extractor_prompt_version,
)
