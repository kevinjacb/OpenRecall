package com.openrecall.relay.data

import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.MemoryListResponseDto
import com.openrecall.relay.http.dto.MemorySearchResponseDto
import com.openrecall.relay.http.dto.MemoryStatsResponseDto
import com.openrecall.relay.http.dto.SessionMemoryResponseDto
import com.openrecall.relay.http.dto.DtoJson
import com.openrecall.relay.http.dto.ErrorEnvelopeDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import java.net.URLEncoder

/**
 * Thin HTTP wrapper for the memory endpoints. Mirrors [AgentApi] but
 * for read-only retrieval + session atom listing.
 */
open class MemoryApi(
    private val baseUrl: String,
    private val token: String,
    private val client: OkHttpClient = OkHttpClient(),
) {

    open suspend fun search(
        query: String,
        sessionId: String? = null,
        limit: Int = 10,
    ): MemorySearchResponseDto = withContext(Dispatchers.IO) {
        val path = buildString {
            append("/memory?q=").append(URLEncoder.encode(query, "UTF-8"))
            if (sessionId != null) {
                append("&session_id=").append(URLEncoder.encode(sessionId, "UTF-8"))
            }
            append("&limit=").append(limit)
        }
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .get()
            .addHeader("Authorization", "Bearer $token")
            .build()
        executeRequest(request, MemorySearchResponseDto.serializer())
    }

    /**
     * `GET /memory` with no `q` — list mode.
     *
     * The same path as [search], because they are the same collection seen
     * two ways and clearing the search box should not switch endpoints. The
     * response shape differs, though: a page + cursor rather than ranked hits
     * + a retrieval trace.
     *
     * [kind] is matched against the raw stored value with no validation. The
     * extractor's kind vocabulary is free-form, so an unknown kind returns an
     * empty page rather than a 400 — which is why the filter chips are built
     * from [stats] rather than hardcoded.
     */
    open suspend fun list(
        kind: String? = null,
        sessionId: String? = null,
        limit: Int = 20,
        cursor: String? = null,
    ): MemoryListResponseDto = withContext(Dispatchers.IO) {
        val path = buildString {
            append("/memory?limit=").append(limit)
            if (!kind.isNullOrBlank()) append("&kind=").append(URLEncoder.encode(kind, "UTF-8"))
            if (sessionId != null) {
                append("&session_id=").append(URLEncoder.encode(sessionId, "UTF-8"))
            }
            if (cursor != null) append("&cursor=").append(URLEncoder.encode(cursor, "UTF-8"))
        }
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + path)
            .get()
            .addHeader("Authorization", "Bearer $token")
            .build()
        executeRequest(request, MemoryListResponseDto.serializer())
    }

    /** `GET /memory/stats` — totals for the header and the kind vocabulary. */
    open suspend fun stats(): MemoryStatsResponseDto = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/memory/stats")
            .get()
            .addHeader("Authorization", "Bearer $token")
            .build()
        executeRequest(request, MemoryStatsResponseDto.serializer())
    }

    open suspend fun sessionAtoms(sessionId: String): SessionMemoryResponseDto =
        withContext(Dispatchers.IO) {
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/sessions/$sessionId/memory")
                .get()
                .addHeader("Authorization", "Bearer $token")
                .build()
            executeRequest(request, SessionMemoryResponseDto.serializer())
        }

    private suspend fun <T> executeRequest(
        request: Request,
        serializer: kotlinx.serialization.KSerializer<T>,
    ): T = withContext(Dispatchers.IO) {
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                val raw = response.body?.string()
                val env = raw?.let { r ->
                    runCatching { DtoJson.decodeFromString(ErrorEnvelopeDto.serializer(), r) }.getOrNull()
                }
                throw HttpApiError(
                    code = ErrorCode.fromHttpStatus(response.code),
                    httpStatus = response.code,
                    message = env?.message ?: "HTTP ${response.code}",
                    requestId = env?.request_id,
                )
            }
            val text = response.body?.string() ?: throw HttpApiError(
                code = ErrorCode.INTERNAL_ERROR,
                httpStatus = response.code,
                message = "empty response",
            )
            try {
                DtoJson.decodeFromString(serializer, text)
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }
}
