package com.opensapien.relay.data

import com.opensapien.relay.http.OpenSapienHttpClient
import com.opensapien.relay.http.dto.SpeakerDto
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Narrow interface for the speakers endpoint, faked in host unit tests
 * (mirrors [SessionApi]). [OpenSapienHttpClient] is `final` and does real OkHttp
 * I/O, so it can't be faked — the repository depends on this interface and
 * tests supply a [FakeSpeakerApi].
 */
interface SpeakerApi {
    suspend fun getSpeakers(): List<SpeakerDto>
    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto
    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all")
}

private class HttpSpeakerApi(private val client: suspend () -> OpenSapienHttpClient) : SpeakerApi {
    override suspend fun getSpeakers(): List<SpeakerDto> = client().getSpeakers()
    override suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
        client().renameSpeaker(speakerId, name)
    override suspend fun reassignSpeaker(fromId: String, toId: String, scope: String) =
        client().reassignSpeaker(fromId, toId, scope)
}

/**
 * Domain layer for speakers. Resolves the current client per call so a
 * re-provision takes effect on the next fetch (same pattern as
 * [AgentRepository]/[MemoryRepository]). Maps DTOs to biometric-free
 * [SpeakerEntry]s for the [SpeakerCache].
 */
class SpeakerRepository(
    private val apiProvider: suspend () -> SpeakerApi,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) {

    /** Convenience constructor pinning a single api (test path). */
    constructor(api: SpeakerApi) : this(apiProvider = { api })

    suspend fun loadSpeakers(): List<SpeakerEntry> = withContext(io) {
        apiProvider().getSpeakers().map { dto ->
            SpeakerEntry(
                speakerId = dto.speakerId,
                name = dto.displayName,
                isWearer = dto.isWearer,
            )
        }
    }

    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerEntry = withContext(io) {
        val dto = apiProvider().renameSpeaker(speakerId, name)
        SpeakerEntry(speakerId = dto.speakerId, name = dto.displayName, isWearer = dto.isWearer)
    }

    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all") = withContext(io) {
        apiProvider().reassignSpeaker(fromId, toId, scope)
    }

    companion object {
        /** Wire a [SpeakerRepository] onto the shared client provider. */
        fun fromClient(clientProvider: suspend () -> OpenSapienHttpClient): SpeakerRepository =
            SpeakerRepository(apiProvider = { HttpSpeakerApi(clientProvider) })
    }
}