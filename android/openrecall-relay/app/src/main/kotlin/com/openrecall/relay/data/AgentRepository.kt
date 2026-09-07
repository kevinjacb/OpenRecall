package com.openrecall.relay.data

import com.openrecall.relay.core.TraceContext
import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.AgentResponseDto
import com.openrecall.relay.http.dto.AtomChipDto
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Domain layer for the agent endpoint. The single importer of
 * [HttpApiError] for the read path (INV-11). The VM / UI see only
 * [AgentOutcome] — never the raw HTTP error.
 *
 * [apiProvider] is a suspend factory that returns the current
 * [AgentApi]. It exists so the repository tracks the latest
 * configured server (the [AgentApi] carries the OkHttp client +
 * bearer token at construction time). The repository resolves the
 * current [AgentApi] on every call, so re-provisioning takes effect
 * on the next /agent request without rebuilding the repository.
 */
open class AgentRepository(private val apiProvider: suspend () -> AgentApi) {

    /** Test/convenience constructor: a repository pinned to a single [AgentApi]. */
    constructor(api: AgentApi) : this(apiProvider = { api })

    open suspend fun ask(
        sessionId: String?,
        text: String,
        limit: Int = 10,
    ): AgentOutcome = withContext(Dispatchers.IO) {
        val dto: AgentResponseDto = try {
            apiProvider().postAgent(sessionId, text, limit)
        } catch (e: HttpApiError) {
            return@withContext AgentOutcome.Error(
                code = e.code,
                message = e.message,
                trace = TraceContext(
                    requestId = e.requestId ?: "",
                ),
            )
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Network/timeout/IO failures (SocketTimeoutException,
            // UnknownHostException, the "not provisioned" IOException) must
            // never escape to the VM — the repository is the single boundary
            // that turns wire errors into AgentOutcome.Error (INV-11). Before
            // this catch, a slow /agent call timed out and the IOException
            // propagated through viewModelScope and crashed the app (the
            // "FATAL EXCEPTION: SocketTimeoutException" incident).
            return@withContext AgentOutcome.Error(
                code = ErrorCode.INTERNAL_ERROR,
                message = e.message ?: e.javaClass.simpleName,
                trace = TraceContext(requestId = ""),
            )
        }
        when (dto.outcome) {
            "return", "return_with_uncertainty" -> AgentOutcome.Answer(
                text = dto.answer.orEmpty(),
                atoms = dto.atoms.map { it.toChip() },
                confidence = dto.confidence ?: 0.0,
                // A missing band means the server never scored/verified this
                // answer at all (e.g. HermesPlanner's no-response and
                // unknown-kind paths carry confidence_band="unverified" but
                // an "answer"-kind RETURN_WITH_UNCERTAINTY carries a raw
                // confidence with no discrete band). Defaulting that to
                // "low" would assert a specific (and possibly wrong) band
                // for an answer we know nothing about; "unverified" says
                // exactly what we know, which is nothing.
                confidenceBand = dto.confidence_band ?: "unverified",
                outcome = if (dto.outcome == "return") AgentOutcomeKind.RETURN
                          else AgentOutcomeKind.RETURN_WITH_UNCERTAINTY,
                trace = TraceContext(
                    requestId = dto.request_id,
                    retrievalTraceId = dto.retrieval_trace_id,
                    auditId = dto.audit_id,
                ),
            )
            "refuse" -> AgentOutcome.Refuse(
                reason = dto.refusal_reason ?: "no_supporting_memory",
                trace = TraceContext(
                    requestId = dto.request_id,
                    retrievalTraceId = dto.retrieval_trace_id,
                    auditId = dto.audit_id,
                ),
            )
            else -> AgentOutcome.Error(
                code = com.openrecall.relay.http.ErrorCode.INTERNAL_ERROR,
                message = "unknown outcome: ${dto.outcome}",
                trace = TraceContext(requestId = dto.request_id),
            )
        }
    }
}

enum class AgentOutcomeKind { RETURN, RETURN_WITH_UNCERTAINTY }

data class AtomChip(
    val atomId: String,
    val sessionId: String,
    val kind: String,
    val text: String,
    val createdAt: String,
    val startMs: Int,
    val score: Double,
)

sealed class AgentOutcome {
    data class Answer(
        val text: String,
        val atoms: List<AtomChip>,
        val confidence: Double,
        val confidenceBand: String,
        val outcome: AgentOutcomeKind,
        val trace: TraceContext,
    ) : AgentOutcome()

    data class Refuse(
        val reason: String,
        val trace: TraceContext,
    ) : AgentOutcome()

    data class Error(
        val code: com.openrecall.relay.http.ErrorCode,
        val message: String,
        val trace: TraceContext,
    ) : AgentOutcome()
}

private fun AtomChipDto.toChip() = AtomChip(
    atomId = atom_id,
    sessionId = session_id,
    kind = kind,
    text = text,
    createdAt = created_at,
    startMs = start_ms,
    score = score,
)
