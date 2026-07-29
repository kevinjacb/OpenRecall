package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.AgentRequestDto
import com.sense.relay.http.dto.AgentResponseDto
import com.sense.relay.http.dto.DtoJson
import com.sense.relay.http.dto.ErrorEnvelopeDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Thin HTTP wrapper for the agent endpoints.
 *
 * This class uses a configurable [OkHttpClient] so tests can pass a
 * fake (MockWebServer) and production wires the real pinned client.
 * Errors are normalised to [HttpApiError] so the [AgentRepository] is
 * the only place that needs to know about error categories (INV-11).
 */
open class AgentApi(
    private val baseUrl: String,
    private val token: String,
    private val client: OkHttpClient = OkHttpClient(),
) {

    open suspend fun postAgent(
        sessionId: String?,
        text: String,
        limit: Int = 10,
    ): AgentResponseDto = withContext(Dispatchers.IO) {
        val payload = AgentRequestDto(
            schema_version = "v1",
            session_id = sessionId,
            text = text,
            limit = limit,
        )
        val body = DtoJson.encodeToString(AgentRequestDto.serializer(), payload)
            .toRequestBody(JSON)
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/agent")
            .post(body)
            .addHeader("Authorization", "Bearer $token")
            .build()
        // The /agent endpoint runs the LLM planner (retrieval + generation)
        // and routinely exceeds OkHttp's 10s default read timeout on a local
        // model — which surfaced as a SocketTimeoutException that escaped the
        // repository and crashed the app. Derive a patient client per call:
        // newBuilder() shares the parent's connection pool + TLS config but
        // overrides only the read timeout, so /status, /sessions, /memory
        // keep the fast default. 60s matches the server's own LLM ceiling.
        val patient = client.newBuilder()
            .readTimeout(60, TimeUnit.SECONDS)
            .build()
        patient.newCall(request).execute().use { response ->
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
                DtoJson.decodeFromString(AgentResponseDto.serializer(), text)
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }
}
