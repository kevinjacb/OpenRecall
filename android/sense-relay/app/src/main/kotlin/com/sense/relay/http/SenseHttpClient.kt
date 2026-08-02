package com.sense.relay.http

import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.dto.CaptureEventDto
import com.sense.relay.http.dto.DtoJson
import com.sense.relay.http.dto.ServerStatusDto
import com.sense.relay.http.dto.SessionDetailsDto
import com.sense.relay.http.dto.SessionEventsDto
import com.sense.relay.http.dto.SessionsPageDto
import com.sense.relay.http.dto.SpeakerDto
import com.sense.relay.http.dto.SpeakersDto
import com.sense.relay.http.dto.RenameSpeakerRequestDto
import com.sense.relay.http.dto.ReassignSpeakerRequestDto
import com.sense.relay.http.dto.RenameSpeakerResponseDto
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.IOException
import java.net.URLEncoder
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SenseHttpClient(
    val baseUrl: String,
    val token: String,
    caPem: String? = null,
) {
    val client: OkHttpClient = buildClient(caPem)

    companion object {
        private val JSON = "application/json; charset=utf-8".toMediaType()
    }

    private fun buildClient(caPem: String?): OkHttpClient {
        val builder = OkHttpClient.Builder()
        if (caPem != null) {
            // Pin a single CA: parse the PEM into an X509Certificate and build a
            // TrustManager that trusts only that CA.
            val tm = pinnedTrustManager(caPem)
            builder.sslSocketFactory(
                buildPinnedSslContext(caPem, tm).socketFactory,
                tm,
            )
        }
        return builder.build()
    }

    private fun pinnedTrustManager(caPem: String): X509TrustManager {
        val pinnedCert = parsePem(caPem)
        // Use the pinned CA as the sole trust anchor in a KeyStore, then let the
        // platform's TrustManagerFactory perform full PKIX path validation against
        // that anchor. This accepts chains whose root is issued by the pinned CA
        // (even when the root itself isn't sent) and rejects everything else.
        val keyStore = KeyStore.getInstance(KeyStore.getDefaultType()).apply {
            load(null)
            setCertificateEntry("sense-ca", pinnedCert)
        }
        val tmf = TrustManagerFactory.getInstance(TrustManagerFactory.getDefaultAlgorithm()).apply {
            init(keyStore)
        }
        return tmf.trustManagers.first { it is X509TrustManager } as X509TrustManager
    }

    private fun buildPinnedSslContext(caPem: String, tm: X509TrustManager): SSLContext {
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf(tm), null)
        return ctx
    }

    private fun parsePem(caPem: String): X509Certificate {
        val cf = CertificateFactory.getInstance("X.509")
        return cf.generateCertificate(caPem.byteInputStream()) as X509Certificate
    }

    private fun req(path: String) = Request.Builder()
        .url(baseUrl.trimEnd('/') + path)
        .addHeader("Authorization", "Bearer $token")
        .build()

    suspend fun health(): Boolean = withContext(Dispatchers.IO) {
        client.newCall(req("/health")).execute().use { it.code in 200..299 }
    }

    /**
     * The WS gateway port the server advertises on `/health`, or null if the
     * server doesn't report one (older server / no gateway running). The HTTP
     * control API and the WS gateway run on separate ports, so the relay can't
     * derive the WS URL by scheme-swap alone — it needs this port. Null lets
     * the caller fall back to the legacy same-port behavior. Minimal regex
     * parse (no JSON dep), matching [serverPubkey].
     */
    suspend fun gatewayPort(): Int? = withContext(Dispatchers.IO) {
        client.newCall(req("/health")).execute().use { resp ->
            if (resp.code !in 200..299) return@use null
            val body = resp.body?.string().orEmpty()
            Regex("\"gatewayPort\"\\s*:\\s*(\\d+)").find(body)?.groupValues?.get(1)?.toIntOrNull()
        }
    }

    suspend fun serverPubkey(): ByteArray = withContext(Dispatchers.IO) {
        client.newCall(req("/provisioning/pubkey")).execute().use { resp ->
            if (resp.code == 401) throw SecurityException("unauthorized")
            if (resp.code !in 200..299) throw IOException("pubkey http ${resp.code}")
            val body = resp.body?.string().orEmpty()
            // body is {"pubkey":"<hex>",...}; parse hex. No JSON dep yet — minimal parse:
            val hex = Regex("\"pubkey\"\\s*:\\s*\"([0-9a-fA-F]+)\"").find(body)?.groupValues?.get(1)
                ?: throw IOException("no pubkey in response")
            require(hex.length == 64) { "pubkey hex not 64 chars" }
            hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray().also {
                require(it.size == 32) { "pubkey not 32 bytes" }
            }
        }
    }

    // ---- Phase 3+ server-touching calls, all wired to the real endpoints.
    // Each follows getStatus's error shape: 401/403 -> SecurityException (the
    // "go to Settings" signal), 404 -> HttpStatusException(404, "session … not
    // found"), other non-2xx -> HttpStatusException(code), body parsed with
    // the shared lenient DtoJson. HttpStatusException carries the code so
    // httpApiError can surface it as ApiError.Http (e.g. 503 -> "Server is
    // starting up") instead of a generic ApiError.Unreachable.

    /** `GET /sessions?limit=N&cursor=…`. The cursor is opaque (unpadded
     *  urlsafe base64 from the server) and URL-encoded defensively. */
    suspend fun listSessions(limit: Int = 20, cursor: String? = null): SessionsPageDto =
        withContext(Dispatchers.IO) {
            val path = buildString {
                append("/sessions?limit=").append(limit)
                if (cursor != null) {
                    append("&cursor=").append(URLEncoder.encode(cursor, "UTF-8"))
                }
            }
            client.newCall(req(path)).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(SessionsPageDto.serializer(), resp.body?.string().orEmpty())
            }
        }

    /** `GET /sessions/{id}` — summary + events in one payload. */
    suspend fun getSession(id: SessionId): SessionDetailsDto =
        withContext(Dispatchers.IO) {
            client.newCall(req("/sessions/${id.value}")).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "session ${id.value} not found")
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(SessionDetailsDto.serializer(), resp.body?.string().orEmpty())
            }
        }

    /** `GET /sessions/{id}/events` — the event list alone (unwrapped from
     *  the `{"events": […]}` envelope). */
    suspend fun getSessionEvents(id: SessionId): List<CaptureEventDto> =
        withContext(Dispatchers.IO) {
            client.newCall(req("/sessions/${id.value}/events")).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "session ${id.value} not found")
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(SessionEventsDto.serializer(), resp.body?.string().orEmpty()).events
            }
        }

    /** `GET /speakers` — the registry minus biometrics. Empty list when disabled. */
    suspend fun getSpeakers(): List<SpeakerDto> =
        withContext(Dispatchers.IO) {
            client.newCall(req("/speakers")).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(SpeakersDto.serializer(), resp.body?.string().orEmpty()).speakers
            }
        }

    /** POST /speakers/{id}/rename — set a speaker's display name; returns the updated speaker. */
    suspend fun renameSpeaker(speakerId: String, name: String): SpeakerDto =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(
                RenameSpeakerRequestDto.serializer(),
                RenameSpeakerRequestDto(name),
            ).toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/speakers/$speakerId/rename")
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "speaker $speakerId not found")
                if (resp.code == 409) throw HttpStatusException(409)
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                DtoJson.decodeFromString(
                    RenameSpeakerResponseDto.serializer(),
                    resp.body?.string().orEmpty(),
                ).speaker
            }
        }

    /** POST /speakers/reassign — move all of fromId's turns/embeddings to toId (v1 scope=all). */
    suspend fun reassignSpeaker(fromId: String, toId: String, scope: String = "all"): Unit =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(
                ReassignSpeakerRequestDto.serializer(),
                ReassignSpeakerRequestDto(fromId, toId, scope),
            ).toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/speakers/reassign")
                .post(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
                if (resp.code == 404) throw HttpStatusException(404, "speaker not found")
                if (resp.code == 409) throw HttpStatusException(409)
                if (resp.code !in 200..299) throw HttpStatusException(resp.code)
                // 204 No Content — nothing to parse.
            }
        }

    /**
     * `GET /status`. Follows [serverPubkey]'s error shape, but treats both
     * 401 AND 403 as a [SecurityException] (the "go to Settings" signal) —
     * `serverPubkey` only maps 401, and a 403 is just as much an auth
     * failure for a bearer-token API. Any other non-2xx is a
     * [HttpStatusException] carrying the code, and the body is parsed with
     * the shared lenient [DtoJson] so a future server field addition doesn't
     * hard-fail. The caller
     * ([PollingStatusRepository] via [httpApiError]) classifies the thrown
     * error into an [com.sense.relay.core.model.ApiError].
     */
    suspend fun getStatus(): ServerStatusDto = withContext(Dispatchers.IO) {
        client.newCall(req("/status")).execute().use { resp ->
            if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
            if (resp.code !in 200..299) throw HttpStatusException(resp.code)
            val body = resp.body?.string().orEmpty()
            DtoJson.decodeFromString(ServerStatusDto.serializer(), body)
        }
    }
}

/**
 * A non-auth, non-2xx HTTP response. Carries the status code so the error
 * classifier ([httpApiError]) can surface it as [com.sense.relay.core.model.ApiError.Http],
 * letting the UI distinguish a 503 ("Server is starting up") from a generic
 * network failure ([com.sense.relay.core.model.ApiError.Unreachable]).
 *
 * Extends [IOException] so existing `catch (IOException)` callers (the
 * repository error paths) keep working unchanged; the classifier matches
 * this subtype before the generic `IOException` branch.
 */
class HttpStatusException(val code: Int, override val message: String = "http $code") : IOException(message)