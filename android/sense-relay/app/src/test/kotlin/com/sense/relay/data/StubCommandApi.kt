package com.sense.relay.data

import com.sense.relay.http.ErrorCode
import com.sense.relay.http.HttpApiError
import com.sense.relay.http.dto.CommandRecordDto
import com.sense.relay.http.dto.DtoJson
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.RequestBody.Companion.toRequestBody

/**
 * A controllable fake for [CommandApi] that lets tests stub the
 * response body for each endpoint independently.
 */
class StubCommandApi(
    private val listResponse: String = "[]",
    private val listStatus: Int = 200,
    private val getResponse: String = """{"command_id":"missing","session_id":"s1","type":"capture_photo","params":{},"issued_at":"2026-07-01T00:00:00Z","expires_at":"2026-07-01T00:30:00Z","status":"PENDING","history":[]}""",
    private val getStatus: Int = 404,
    private val ackResponse: String = """{"command_id":"c1","session_id":"s1","type":"capture_photo","params":{},"issued_at":"2026-07-01T00:00:00Z","expires_at":"2026-07-01T00:30:00Z","status":"PENDING","history":[]}""",
    private val ackStatus: Int = 200,
) : CommandApi(
    baseUrl = "http://test",
    token = "t",
    client = OkHttpClient(),
) {
    override suspend fun listActive(): List<CommandRecordDto> = respond(listResponse, listStatus) {
        DtoJson.decodeFromString(
            kotlinx.serialization.builtins.ListSerializer(CommandRecordDto.serializer()),
            it,
        )
    }

    override suspend fun get(commandId: String): CommandRecordDto = respond(getResponse, getStatus) {
        DtoJson.decodeFromString(CommandRecordDto.serializer(), it)
    }

    override suspend fun ack(commandId: String): CommandRecordDto = respond(ackResponse, ackStatus) {
        DtoJson.decodeFromString(CommandRecordDto.serializer(), it)
    }

    private suspend fun <T> respond(
        body: String,
        status: Int,
        parse: (String) -> T,
    ): T {
        if (status >= 400) {
            throw HttpApiError(
                code = ErrorCode.INTERNAL_ERROR,
                httpStatus = status,
                message = body,
            )
        }
        return parse(body)
    }
}
