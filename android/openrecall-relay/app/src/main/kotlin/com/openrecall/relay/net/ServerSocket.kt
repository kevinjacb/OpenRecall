package com.openrecall.relay.net

import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString
import okio.ByteString.Companion.toByteString
import java.util.concurrent.TimeUnit

/**
 * WebSocket to the gateway. Carries the §E control plane (text) and forwards §C.6
 * audio (binary) up; receives §E control (text) down. A thin transport around the
 * relay brain — no protocol logic here.
 */
class ServerSocket(
    private val url: String,
    private val token: String,
    private val listener: Listener,
    /**
     * Test seam: a factory for the underlying [WebSocket]. Production leaves
     * it null (uses the OkHttp client); tests inject a fake whose [WebSocket.send]
     * returns a controlled result so [sendBinary]/[sendText]'s Boolean return can
     * be exercised without a real socket.
     */
    private val newSocket: ((Request, WebSocketListener) -> WebSocket)? = null,
) {

    interface Listener {
        fun onOpen()
        fun onText(text: String)
        fun onClosed(reason: String)
    }

    // No read timeout: the connection is long-lived and mostly idle between messages.
    private val client = OkHttpClient.Builder()
        .pingInterval(20, TimeUnit.SECONDS)   // keepalive; also detects dead links
        .readTimeout(0, TimeUnit.MILLISECONDS)
        .build()

    private var socket: WebSocket? = null

    fun connect() {
        val req = Request.Builder().url(url)
            .addHeader("Authorization", "Bearer $token")
            .build()
        val wsListener = object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) = listener.onOpen()
            override fun onMessage(webSocket: WebSocket, text: String) = listener.onText(text)
            override fun onMessage(webSocket: WebSocket, bytes: ByteString) { /* server->relay is text only */ }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
                listener.onClosed(reason.ifEmpty { "closed" })
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                listener.onClosed(t.message ?: "failure")
        }
        socket = (newSocket ?: { r, l -> client.newWebSocket(r, l) })(req, wsListener)
    }

    /**
     * Send a §E text frame. Returns the OkHttp [WebSocket.send] result: `true`
     * if enqueued, `false` if the socket is closed/closing (a dead-link signal
     * the caller routes to its drop path). Before [connect] (socket null)
     * returns `false`. A2: was Unit / ignored — a dropped WS frame was invisible.
     */
    fun sendText(text: String): Boolean = socket?.send(text) ?: false

    /**
     * Forward a §C.6 audio packet (binary). Returns the OkHttp [WebSocket.send]
     * result: `true` if enqueued, `false` if the socket is closed/closing — a
     * dropped WS frame is now visible to the caller instead of silently lost.
     * Before [connect] (socket null) returns `false`.
     */
    fun sendBinary(data: ByteArray): Boolean = socket?.send(data.toByteString()) ?: false

    fun close() { socket?.close(1000, "bye") }
}
