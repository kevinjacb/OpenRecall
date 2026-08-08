package com.openrecall.relay.data

import com.openrecall.relay.domain.model.MemoryStats
import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.MemoryAtomDto
import com.openrecall.relay.http.dto.toDomain

/**
 * Domain layer for the memory endpoints. Maps [HttpApiError] to a
 * sealed [MemoryOutcome] so the VM sees only domain types.
 *
 * [apiProvider] is a suspend factory that returns the current
 * [MemoryApi] (i.e. with the live (baseUrl, token, client) resolved
 * from the shared [com.openrecall.relay.http.OpenRecallHttpClient] provider).
 * The provider is called on every call, so a re-provision in Settings takes
 * effect on the next `/memory` request without rebuilding the repository.
 * This mirrors [CommandRepository].
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
                atoms = dto.atoms.map { it.toMemoryAtom() },
                query = dto.query,
                retrievalTraceId = dto.retrieval_trace_id,
            )
        } catch (e: HttpApiError) {
            MemoryOutcome.Error(e.code, e.message)
        }
    }

    /**
     * Browse mode — `GET /memory` with no query.
     *
     * This is what the Memories tab renders by default. It is a genuinely
     * different call from [search], not an empty search: it is ordered by
     * conversation time, filterable by kind, and paged, whereas search is
     * ranked by embedding similarity and unpaged.
     *
     * [cursor] is the opaque `next_cursor` from a previous page.
     */
    suspend fun list(
        kind: String? = null,
        sessionId: String? = null,
        limit: Int = 20,
        cursor: String? = null,
    ): MemoryPageOutcome {
        return try {
            val dto = apiProvider().list(kind, sessionId, limit, cursor)
            MemoryPageOutcome.Success(
                atoms = dto.atoms.map { it.toMemoryAtom() },
                nextCursor = dto.next_cursor,
            )
        } catch (e: HttpApiError) {
            MemoryPageOutcome.Error(e.code, e.message)
        }
    }

    /** Totals for the header, and the kind vocabulary for the filter chips. */
    suspend fun stats(): StatsOutcome {
        return try {
            StatsOutcome.Success(apiProvider().stats().toDomain())
        } catch (e: HttpApiError) {
            StatsOutcome.Error(e.code, e.message)
        }
    }

    suspend fun sessionAtoms(sessionId: String): MemoryOutcome {
        return try {
            val dto = apiProvider().sessionAtoms(sessionId)
            MemoryOutcome.Success(
                atoms = dto.atoms.map { it.toMemoryAtom() },
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
    /**
     * When this was said, as opposed to when the server extracted it. Null
     * for atoms written before the server carried the field. Ordering and
     * "added today" counts use this; [createdAt] is only for debugging.
     */
    val occurredAt: String?,
    val startMs: Int,
    val sourceEventId: String,
    val sourceModality: String,
    val extractionVersion: String,
    val embeddingModel: String,
    val extractorPromptVersion: String,
) {
    /** The timestamp to display: conversation time when the server knows it,
     *  else the extraction time, which is the only thing left. */
    val displayedAt: String get() = occurredAt ?: createdAt
}

sealed class MemoryOutcome {
    data class Success(
        val atoms: List<MemoryAtom>,
        val query: String,
        val retrievalTraceId: String,
    ) : MemoryOutcome()
    data class Error(val code: ErrorCode, val message: String) : MemoryOutcome()
}

/** A page of browsed atoms. Distinct from [MemoryOutcome] because only the
 *  list mode carries a cursor. */
sealed class MemoryPageOutcome {
    data class Success(
        val atoms: List<MemoryAtom>,
        val nextCursor: String?,
    ) : MemoryPageOutcome()
    data class Error(val code: ErrorCode, val message: String) : MemoryPageOutcome()
}

sealed class StatsOutcome {
    data class Success(val stats: MemoryStats) : StatsOutcome()
    data class Error(val code: ErrorCode, val message: String) : StatsOutcome()
}

internal fun MemoryAtomDto.toMemoryAtom() = MemoryAtom(
    atomId = atom_id,
    sessionId = session_id,
    kind = kind,
    text = text,
    createdAt = created_at,
    occurredAt = occurred_at,
    startMs = start_ms,
    sourceEventId = source_event_id,
    sourceModality = source_modality,
    extractionVersion = extraction_version,
    embeddingModel = embedding_model,
    extractorPromptVersion = extractor_prompt_version,
)
