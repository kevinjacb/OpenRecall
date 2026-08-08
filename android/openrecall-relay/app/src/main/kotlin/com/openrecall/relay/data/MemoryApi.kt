package com.openrecall.relay.data

import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.MemoryAtomDto
import com.openrecall.relay.http.dto.MemorySearchResponseDto
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
