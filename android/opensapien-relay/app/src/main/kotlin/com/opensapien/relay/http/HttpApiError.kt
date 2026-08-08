package com.opensapien.relay.http

/**
 * The binding error category for the agent + memory endpoints.
 *
 * INV-11 (error category discipline): only [AgentRepository] and
 * [MemoryRepository] translate this exception into a domain outcome;
 * every other layer passes it through unchanged.
 *
 * Codes are the binding enum from the server's [ErrorEnvelopeDTO]:
 *   - bad_request: malformed request body (400)
 *   - unauthorized: missing / invalid bearer token (401)
 *   - not_found: unknown session / atom (404)
 *   - rate_limited: too many requests (429)
 *   - internal_error: anything else (5xx)
 */
class HttpApiError(
    val code: ErrorCode,
    val httpStatus: Int,
    override val message: String,
    val requestId: String? = null,
) : RuntimeException("[$code @ $httpStatus] $message")

enum class ErrorCode {
    BAD_REQUEST,
    UNAUTHORIZED,
    NOT_FOUND,
    RATE_LIMITED,
    INTERNAL_ERROR;

    companion object {
        fun fromHttpStatus(status: Int): ErrorCode = when (status) {
            400 -> BAD_REQUEST
            401 -> UNAUTHORIZED
            404 -> NOT_FOUND
            429 -> RATE_LIMITED
            else -> INTERNAL_ERROR
        }
    }
}
