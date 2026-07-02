package com.sense.relay.http

import okhttp3.OkHttpClient
import okhttp3.Request
import java.io.ByteArrayInputStream
import java.io.IOException
import java.security.cert.CertificateException
import java.security.cert.CertificateFactory
import java.security.cert.X509Certificate
import javax.net.ssl.SSLContext
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
        return object : X509TrustManager {
            override fun checkClientTrusted(chain: Array<out X509Certificate>, authType: String) {
                throw CertificateException("client auth not supported")
            }

            override fun checkServerTrusted(chain: Array<out X509Certificate>, authType: String) {
                // Accept only chains that anchor on the pinned CA: either the pinned
                // cert is present in the chain, or the chain's root equals the pinned cert.
                val anchored = chain.any { it == pinnedCert } ||
                    chain.lastOrNull() == pinnedCert
                if (!anchored) {
                    throw CertificateException("chain does not anchor on pinned CA")
                }
            }

            override fun getAcceptedIssuers(): Array<X509Certificate> = arrayOf(pinnedCert)
        }
    }

    private fun buildPinnedSslContext(caPem: String, tm: X509TrustManager): SSLContext {
        val ctx = SSLContext.getInstance("TLS")
        ctx.init(null, arrayOf(tm), null)
        return ctx
    }

    private fun parsePem(caPem: String): X509Certificate {
        val cf = CertificateFactory.getInstance("X.509")
        return cf.generateCertificate(ByteArrayInputStream(caPem.toByteArray(Charsets.US_ASCII)))
            as X509Certificate
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
            hex.chunked(2).map { it.toInt(16).toByte() }.toByteArray().also {
                require(it.size == 32) { "pubkey not 32 bytes" }
            }
        }
    }
}