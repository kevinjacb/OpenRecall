package com.sense.relay.http

import com.sense.relay.domain.model.SessionId
import com.sense.relay.http.dto.CaptureEventDto
import com.sense.relay.http.dto.DtoJson
import com.sense.relay.http.dto.ServerStatusDto
import com.sense.relay.http.dto.SessionDetailsDto
import com.sense.relay.http.dto.SessionsPageDto
import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.IOException
import java.security.KeyStore
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
import javax.net.ssl.TrustManagerFactory
import javax.net.ssl.X509TrustManager
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext

class SenseHttpClient(
    private val baseUrl: String,
    private val token: String,
    caPem: String? = null,
) {
    private val client: OkHttpClient = buildClient(caPem)

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

    // ---- Phase 3+ server-touching calls. `getStatus` is wired to the real
    // `GET /status` in Phase 4 (the first call the main-app UI needs); the
    // session-listing/detail calls land in Phase 5. The still-stubbed methods
    // throw an IOException whose message names the method and the phase, so a
    // missing-wiring logcat line is greppable.

    suspend fun listSessions(limit: Int = 20, cursor: String? = null): SessionsPageDto =
        stub("listSessions")

    suspend fun getSession(id: SessionId): SessionDetailsDto =
        stub("getSession(${id.value})")

    suspend fun getSessionEvents(id: SessionId): List<CaptureEventDto> =
        stub("getSessionEvents(${id.value})")

    /**
     * `GET /status`. Mirrors [serverPubkey]'s shape: a 401/403 is a
     * [SecurityException] (the "go to Settings" signal), any other non-2xx
     * is an [IOException], and the body is parsed with the shared lenient
     * [DtoJson] so a future server field addition doesn't hard-fail. The
     * caller ([PollingStatusRepository] via [statusApiError]) classifies the
     * thrown error into an [com.sense.relay.core.model.ApiError].
     */
    suspend fun getStatus(): ServerStatusDto = withContext(Dispatchers.IO) {
        client.newCall(req("/status")).execute().use { resp ->
            if (resp.code == 401 || resp.code == 403) throw SecurityException("unauthorized")
            if (resp.code !in 200..299) throw IOException("status http ${resp.code}")
            val body = resp.body?.string().orEmpty()
            DtoJson.decodeFromString(ServerStatusDto.serializer(), body)
        }
    }

    private fun stub(method: String): Nothing =
        throw IOException("$method: not yet wired (Phase 5 server endpoint)")
}