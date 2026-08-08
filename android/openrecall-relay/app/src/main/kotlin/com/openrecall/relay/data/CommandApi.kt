package com.openrecall.relay.data

import com.openrecall.relay.http.ErrorCode
import com.openrecall.relay.http.HttpApiError
import com.openrecall.relay.http.dto.CommandRecordDto
import com.openrecall.relay.http.dto.CreateCommandRequestDto
import com.openrecall.relay.http.dto.DtoJson
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * Thin HTTP wrapper for the /commands endpoints.
 *
 * The Android command-lifecycle UI consumes these:
 * - GET /commands → list active (non-terminal) commands
 * - GET /commands/{id} → single command with full history
 * - POST /commands/{id}/ack → device ack (DELIVERED → EXECUTING)
 *
 * Mirrors AgentApi / MemoryApi style: lazy-injectable, errors
 * normalized to HttpApiError so the repository is the only place
 * that knows about error categories (INV-11).
 */
open class CommandApi(
    private val baseUrl: String,
    private val token: String,
    private val client: OkHttpClient = OkHttpClient(),
) {

    open suspend fun listActive(): List<CommandRecordDto> = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/commands")
            .get()
            .addHeader("Authorization", "Bearer $token")
            .build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) {
                throw httpError(response.code, response.body?.string())
            }
            val text = response.body?.string() ?: "[]"
            try {
                DtoJson.decodeFromString(
                    kotlinx.serialization.builtins.ListSerializer(CommandRecordDto.serializer()),
                    text,
                )
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }

    /**
     * `POST /commands` — issue a command to the wearable.
     *
     * Until the server grew this route, commands could only be created by the
     * agent's internal planner path, so the app had no way to act on the
     * device at all. It goes through the same validate → guardrails → sign
     * chain the planner uses.
     *
     * No `session_id` is sent: session ids are relay-minted UUIDs never
     * surfaced over HTTP, so the client has nothing truthful to put there.
     * An absent value means "the device, whenever it is next connected" and
     * the gateway stamps the live session id at delivery.
     *
     * [idempotencyKey] must be stable across retries of the *same* intent —
     * the server dedupes on it, and a fresh key on a retry issues the command
     * twice. A guardrail refusal comes back as 403, which is a "the device
     * can't do this right now" answer rather than a malformed request.
     */
    open suspend fun create(
        type: String,
        idempotencyKey: String,
        params: Map<String, String> = emptyMap(),
    ): CommandRecordDto = withContext(Dispatchers.IO) {
        val body = DtoJson.encodeToString(
            CreateCommandRequestDto.serializer(),
            CreateCommandRequestDto(
                type = type,
                idempotency_key = idempotencyKey,
                params = params,
            ),
        ).toRequestBody("application/json".toMediaType())
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/commands")
            .post(body)
            .addHeader("Authorization", "Bearer $token")
            .build()
        client.newCall(request).execute().use { response ->
            if (response.code == 403) {
                throw HttpApiError(
                    code = ErrorCode.BAD_REQUEST,
                    httpStatus = 403,
                    message = response.body?.string() ?: "the device can't run that right now",
                )
            }
            if (!response.isSuccessful) {
                throw httpError(response.code, response.body?.string())
            }
            val text = response.body?.string() ?: ""
            try {
                DtoJson.decodeFromString(CommandRecordDto.serializer(), text)
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }

    open suspend fun get(commandId: String): CommandRecordDto = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/commands/$commandId")
            .get()
            .addHeader("Authorization", "Bearer $token")
            .build()
        client.newCall(request).execute().use { response ->
            if (response.code == 404) {
                throw HttpApiError(
                    code = ErrorCode.NOT_FOUND,
                    httpStatus = 404,
                    message = "command '$commandId' not found",
                )
            }
            if (!response.isSuccessful) {
                throw httpError(response.code, response.body?.string())
            }
            val text = response.body?.string() ?: ""
            try {
                DtoJson.decodeFromString(CommandRecordDto.serializer(), text)
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }

    open suspend fun ack(commandId: String): CommandRecordDto = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/commands/$commandId/ack")
            .post("".toRequestBody("application/json".toMediaType()))
            .addHeader("Authorization", "Bearer $token")
            .build()
        client.newCall(request).execute().use { response ->
            if (response.code == 404) {
                throw HttpApiError(
                    code = ErrorCode.NOT_FOUND,
                    httpStatus = 404,
                    message = "command '$commandId' not found",
                )
            }
            if (response.code == 409) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = 409,
                    message = response.body?.string() ?: "conflict",
                )
            }
            if (!response.isSuccessful) {
                throw httpError(response.code, response.body?.string())
            }
            val text = response.body?.string() ?: ""
            try {
                DtoJson.decodeFromString(CommandRecordDto.serializer(), text)
            } catch (e: Exception) {
                throw HttpApiError(
                    code = ErrorCode.INTERNAL_ERROR,
                    httpStatus = response.code,
                    message = "malformed response: ${e.message}",
                )
            }
        }
    }

    private fun httpError(status: Int, body: String?): HttpApiError {
        val (code, msg) = when (status) {
            401 -> ErrorCode.UNAUTHORIZED to "unauthorized"
            404 -> ErrorCode.NOT_FOUND to "not found"
            429 -> ErrorCode.RATE_LIMITED to "rate limited"
            else -> ErrorCode.INTERNAL_ERROR to (body ?: "HTTP $status")
        }
        return HttpApiError(code = code, httpStatus = status, message = msg)
    }
}
