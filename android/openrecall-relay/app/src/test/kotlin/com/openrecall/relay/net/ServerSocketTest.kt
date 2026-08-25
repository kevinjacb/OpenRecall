package com.openrecall.relay.net

import kotlin.test.Test
import kotlin.test.assertEquals
import kotlin.test.assertFalse
import kotlin.test.assertTrue
import okhttp3.Request
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okio.ByteString

/**
 * Pins the A2 fail-fast contract on [ServerSocket]: `sendBinary`/`sendText`
 * return the OkHttp [WebSocket.send] Boolean so a dropped WS frame is visible
 * to the caller (was Unit / ignored). A false return is the dead-link signal
 * [com.openrecall.relay.RelayService] routes to its drop path.
 *
 * The underlying [WebSocket] is injected via [ServerSocket]'s `newSocket`
 * factory seam so a fake whose `send()` returns a controlled result replaces
 * the real socket without a network.
 */
class ServerSocketTest {

    /** A minimal OkHttp [WebSocket] fake whose `send` returns a fixed Boolean. */
    private class FakeSocket(val sendOk: Boolean) : WebSocket {
        override fun send(text: String): Boolean = sendOk
        override fun send(bytes: ByteString): Boolean = sendOk
        override fun close(code: Int, reason: String?): Boolean = true
        override fun cancel() = Unit
        override fun queueSize(): Long = 0L
        override fun request(): Request = Request.Builder().url("http://localhost").build()
    }

    private val noopListener = object : ServerSocket.Listener {
        override fun onOpen() = Unit
        override fun onText(text: String) = Unit
        override fun onClosed(reason: String) = Unit
    }

    private fun socketWith(sendOk: Boolean): ServerSocket =
        ServerSocket("ws://localhost", "tok", noopListener) { _, _ -> FakeSocket(sendOk) }

    @Test
    fun sendBinary_returnsFalseWhenTheWebSocketSendFails() {
        val s = socketWith(sendOk = false).also { it.connect() }
        assertFalse(s.sendBinary(byteArrayOf(1, 2, 3)), "a failed send must return false, not be ignored")
    }

    @Test
    fun sendBinary_returnsTrueWhenTheWebSocketSendSucceeds() {
        val s = socketWith(sendOk = true).also { it.connect() }
        assertTrue(s.sendBinary(byteArrayOf(1, 2, 3)))
    }

    @Test
    fun sendText_returnsFalseWhenTheWebSocketSendFails() {
        val s = socketWith(sendOk = false).also { it.connect() }
        assertFalse(s.sendText("hello"))
    }

    @Test
    fun sendText_returnsTrueWhenTheWebSocketSendSucceeds() {
        val s = socketWith(sendOk = true).also { it.connect() }
        assertTrue(s.sendText("hello"))
    }

    @Test
    fun sendBinary_returnsFalseBeforeConnect_soTheCallerSeesNoSocketNotACrash() {
        // socket is null before connect() — a send returns false (dead link),
        // not an exception. The caller's null-guard treats this as "already
        // torn down" and skips the drop path; either way it must not crash.
        val s = ServerSocket("ws://localhost", "tok", noopListener)
        assertFalse(s.sendBinary(byteArrayOf(1)))
        assertFalse(s.sendText("hi"))
    }

    @Test
    fun sendBinary_returnsTheSendResultForEveryCallNotJustTheFirst() {
        // A socket that fails on every send must report false every time, so a
        // relay streaming into a dead socket sees the failure on each frame.
        val s = socketWith(sendOk = false).also { it.connect() }
        assertEquals(false, s.sendBinary(byteArrayOf(1)))
        assertEquals(false, s.sendBinary(byteArrayOf(2)))
        assertEquals(false, s.sendText("x"))
    }
}