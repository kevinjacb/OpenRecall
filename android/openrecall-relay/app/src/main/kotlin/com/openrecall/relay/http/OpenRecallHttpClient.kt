package com.openrecall.relay.http

import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.http.dto.CaptureEventDto
import com.openrecall.relay.http.dto.DeviceStatusDto
import com.openrecall.relay.http.dto.DtoJson
import com.openrecall.relay.http.dto.PatchSegmentRequestDto
import com.openrecall.relay.http.dto.SegmentDetailsDto
import com.openrecall.relay.http.dto.SegmentMemoryDto
import com.openrecall.relay.http.dto.SegmentSummaryDto
import com.openrecall.relay.http.dto.SegmentsPageDto
import com.openrecall.relay.http.dto.ServerStatusDto
import com.openrecall.relay.http.dto.SessionDetailsDto
import com.openrecall.relay.http.dto.SessionEventsDto
import com.openrecall.relay.http.dto.SessionsPageDto
import com.openrecall.relay.http.dto.SettingsDocumentDto
import com.openrecall.relay.http.dto.SpeakerDto
import com.openrecall.relay.http.dto.SpeakersDto
import com.openrecall.relay.http.dto.RenameSpeakerRequestDto
import com.openrecall.relay.http.dto.ReassignSpeakerRequestDto
import com.openrecall.relay.http.dto.RenameSpeakerResponseDto
import com.openrecall.relay.http.dto.WaveformDto
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

class OpenRecallHttpClient(
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
            setCertificateEntry("openrecall-ca", pinnedCert)
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

    /**
     * URL-encode one path segment or query value. Segment ids are
     * `"<session_id>:<seq>"`, and cursors are opaque base64url — neither is
     * safe to splice into a URL raw.
     */
    private fun enc(value: String): String = URLEncoder.encode(value, "UTF-8")

    /**
     * The shared non-2xx policy for the calls above: 401/403 →
     * [SecurityException] (the "go to Settings" signal), 404 → a
     * [HttpStatusException] carrying a caller-supplied message, anything else
     * → [HttpStatusException] with the code. The body is decoded with the
     * lenient [DtoJson] so a server field addition never hard-fails.
     */
    private fun <T> parse(
        resp: okhttp3.Response,
        serializer: kotlinx.serialization.KSerializer<T>,
        notFound: String? = null,
    ): T {
        if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
        if (resp.code == 404 && notFound != null) throw HttpStatusException(404, notFound)
        if (resp.code !in 200..299) throw HttpStatusException(resp.code)
        return DtoJson.decodeFromString(serializer, resp.body?.string().orEmpty())
    }

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

    // ---- Segments: the app's primary read surface (spec §2.3). `/sessions`
    // above stays for plumbing and diagnostics; a session is the foreground
    // service's lifetime, not a recording, so nothing user-facing addresses it.

    /**
     * `GET /segments?limit&cursor&q&session_id`.
     *
     * With `q`, the server runs a transcript substring search and returns one
     * deduped row per matching segment with a `matchSnippet`. Search mode is
     * unpaginated — `nextCursor` is always null — so the caller must not treat
     * a missing cursor as "the list ended" while a query is active.
     */
    suspend fun listSegments(
        limit: Int = 20,
        cursor: String? = null,
        query: String? = null,
        sessionId: String? = null,
    ): SegmentsPageDto = withContext(Dispatchers.IO) {
        val path = buildString {
            append("/segments?limit=").append(limit)
            if (cursor != null) append("&cursor=").append(enc(cursor))
            if (!query.isNullOrBlank()) append("&q=").append(enc(query))
            if (sessionId != null) append("&session_id=").append(enc(sessionId))
        }
        client.newCall(req(path)).execute().use { resp -> parse(resp, SegmentsPageDto.serializer()) }
    }

    /** `GET /segments/{id}` — summary + the segment's slice of the event stream. */
    suspend fun getSegment(id: SegmentId): SegmentDetailsDto = withContext(Dispatchers.IO) {
        client.newCall(req("/segments/${enc(id.value)}")).execute().use { resp ->
            parse(resp, SegmentDetailsDto.serializer(), notFound = "recording not found")
        }
    }

    /** `GET /segments/{id}/memory` — the "memories from this recording" chips. */
    suspend fun getSegmentMemory(id: SegmentId): SegmentMemoryDto = withContext(Dispatchers.IO) {
        client.newCall(req("/segments/${enc(id.value)}/memory")).execute().use { resp ->
            parse(resp, SegmentMemoryDto.serializer(), notFound = "recording not found")
        }
    }

    /** `GET /segments/{id}/waveform` — 500 ms peak buckets for the scrubber.
     *  404 when the segment has no audio, which is a normal state, not an error. */
    suspend fun getSegmentWaveform(id: SegmentId): WaveformDto = withContext(Dispatchers.IO) {
        client.newCall(req("/segments/${enc(id.value)}/waveform")).execute().use { resp ->
            parse(resp, WaveformDto.serializer(), notFound = "no audio for this recording")
        }
    }

    /** `PATCH /segments/{id}` — rename. The server records the title as
     *  user-authored, which permanently protects it from the auto-titler. */
    suspend fun renameSegment(id: SegmentId, title: String): SegmentSummaryDto =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(
                PatchSegmentRequestDto.serializer(),
                PatchSegmentRequestDto(title),
            ).toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/segments/${enc(id.value)}")
                .patch(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                parse(resp, SegmentSummaryDto.serializer(), notFound = "recording not found")
            }
        }

    /**
     * `DELETE /segments/{id}` — cascades to transcript events, memory atoms,
     * their vectors and the audio range.
     *
     * 409 means the segment is still recording: it is actively being written
     * to, so the server refuses rather than racing the ingest path. The caller
     * surfaces that as "still recording", not as a generic failure.
     */
    suspend fun deleteSegment(id: SegmentId): Unit = withContext(Dispatchers.IO) {
        val request = Request.Builder()
            .url(baseUrl.trimEnd('/') + "/segments/${enc(id.value)}")
            .delete()
            .addHeader("Authorization", "Bearer $token")
            .build()
        client.newCall(request).execute().use { resp ->
            if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
            if (resp.code == 404) throw HttpStatusException(404, "recording not found")
            if (resp.code == 409) throw HttpStatusException(409, "still recording")
            if (resp.code !in 200..299) throw HttpStatusException(resp.code)
            // 204 No Content — nothing to parse.
        }
    }

    /**
     * The URL the media player streams from. Not fetched here: playback is the
     * platform [android.media.MediaPlayer]'s job, and it needs the URL plus the
     * bearer header rather than bytes. The server serves this with
     * `FileResponse`, so Range requests and `206` work and scrubbing does not
     * require downloading the whole recording.
     */
    fun segmentAudioUrl(id: SegmentId): String =
        baseUrl.trimEnd('/') + "/segments/${enc(id.value)}/audio"

    // ---- Settings and device status (spec §4.1, §5.1) -----------------------

    /** `GET /settings` — the relay's durable desired state. */
    suspend fun getSettings(): SettingsDocumentDto = withContext(Dispatchers.IO) {
        client.newCall(req("/settings")).execute().use { resp ->
            parse(resp, SettingsDocumentDto.serializer())
        }
    }

    /** `PUT /settings` — send the full merged document; the server returns the
     *  result of merging it. A 400 means a key the server doesn't know, which
     *  is a client bug rather than something to retry. */
    suspend fun putSettings(document: SettingsDocumentDto): SettingsDocumentDto =
        withContext(Dispatchers.IO) {
            val body = DtoJson.encodeToString(SettingsDocumentDto.serializer(), document)
                .toRequestBody(JSON)
            val request = Request.Builder()
                .url(baseUrl.trimEnd('/') + "/settings")
                .put(body)
                .addHeader("Authorization", "Bearer $token")
                .build()
            client.newCall(request).execute().use { resp ->
                parse(resp, SettingsDocumentDto.serializer())
            }
        }

    /** `GET /device/status`. */
    suspend fun getDeviceStatus(): DeviceStatusDto = withContext(Dispatchers.IO) {
        client.newCall(req("/device/status")).execute().use { resp ->
            parse(resp, DeviceStatusDto.serializer())
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
     * error into an [com.openrecall.relay.core.model.ApiError].
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
 * classifier ([httpApiError]) can surface it as [com.openrecall.relay.core.model.ApiError.Http],
 * letting the UI distinguish a 503 ("Server is starting up") from a generic
 * network failure ([com.openrecall.relay.core.model.ApiError.Unreachable]).
 *
 * Extends [IOException] so existing `catch (IOException)` callers (the
 * repository error paths) keep working unchanged; the classifier matches
 * this subtype before the generic `IOException` branch.
 */
class HttpStatusException(val code: Int, override val message: String = "http $code") : IOException(message)