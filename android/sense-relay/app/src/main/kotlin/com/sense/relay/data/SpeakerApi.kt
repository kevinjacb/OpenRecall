package com.sense.relay.data

import com.sense.relay.http.SenseHttpClient
import com.sense.relay.http.dto.SpeakerDto
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

/**
 * Narrow interface for the speakers endpoint, faked in host unit tests
 * (mirrors [SessionApi]). [SenseHttpClient] is `final` and does real OkHttp
 * I/O, so it can't be faked — the repository depends on this interface and
 * tests supply a [FakeSpeakerApi].
 */
interface SpeakerApi {
    suspend fun getSpeakers(): List<SpeakerDto>
}

private class HttpSpeakerApi(private val client: suspend () -> SenseHttpClient) : SpeakerApi {
    override suspend fun getSpeakers(): List<SpeakerDto> = client().getSpeakers()
}

/**
 * Domain layer for speakers. Resolves the current client per call so a
 * re-provision takes effect on the next fetch (same pattern as
 * [AgentRepository]/[MemoryRepository]). Maps DTOs to biometric-free
 * [SpeakerEntry]s for the [SpeakerCache].
 */
class SpeakerRepository(private val apiProvider: suspend () -> SpeakerApi) {

    /** Convenience constructor pinning a single api (test path). */
    constructor(api: SpeakerApi) : this(apiProvider = { api })

    suspend fun loadSpeakers(): List<SpeakerEntry> = withContext(Dispatchers.IO) {
        apiProvider().getSpeakers().map { dto ->
            SpeakerEntry(
                speakerId = dto.speakerId,
                name = dto.displayName,
                isWearer = dto.isWearer,
            )
        }
    }

    companion object {
        /** Wire a [SpeakerRepository] onto the shared client provider. */
        fun fromClient(clientProvider: suspend () -> SenseHttpClient): SpeakerRepository =
            SpeakerRepository(apiProvider = { HttpSpeakerApi(clientProvider) })
    }
}