package com.sense.relay.net

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
class ServerSocket(private val url: String, private val listener: Listener) {

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
        socket = client.newWebSocket(
            Request.Builder().url(url).build(),
            object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) = listener.onOpen()
                override fun onMessage(webSocket: WebSocket, text: String) = listener.onText(text)
                override fun onMessage(webSocket: WebSocket, bytes: ByteString) { /* server->relay is text only */ }
                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) =
                    listener.onClosed(reason.ifEmpty { "closed" })
                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) =
                    listener.onClosed(t.message ?: "failure")
            },
        )
    }

    fun sendText(text: String) { socket?.send(text) }
    fun sendBinary(data: ByteArray) { socket?.send(data.toByteString()) }
    fun close() { socket?.close(1000, "bye") }
}
