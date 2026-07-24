package com.sense.relay

import com.sense.relay.data.AtomChip
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.ChatMessageKind
import com.sense.relay.data.Role
import com.sense.relay.protocol.CommandAck
import com.sense.relay.protocol.Bye
import com.sense.relay.protocol.Hello
import com.sense.relay.protocol.ServerMessage
import com.sense.relay.protocol.encode
import com.sense.relay.protocol.parseServerMessage
import java.util.Base64

/**
 * The relay's protocol brain: bridges one BLE device session to one WebSocket server
 * session. Pure and I/O-free — it consumes events and returns [RelayAction]s for the
 * runtime ([RelayService]) to execute. This mirrors the server's `GatewayCore` and is
 * the executable spec for the relay's behaviour (see RelaySessionTest).
 *
 * Tier split: the device speaks only §C.6 audio + §D commands over BLE; THIS layer
 * owns §E (hello/bye/command_ack) and the BLE<->WS translation, including turning a
 * server `command` (base64 sig + JSON payload) into the device's on-wire command
 * frame `[raw 64-byte signature][payload JSON]`.
 */
class RelaySession(val sessionId: String, private val startSeq: Int = 0) {

    /**
     * True once [start] has emitted `hello` for the current link. The BLE
     * (GATT) thread can deliver [onDeviceAudio] before the WS reader thread
     * fires `onOpen` → [start], and OkHttp transmits any pre-`onOpen` sends in
     * FIFO order — so ungated audio would reach the server *before* `hello`
     * and the server would close 1002 ("audio received before hello"). We hold
     * such audio here and flush it after `hello` so `hello` is always first.
     */
    private var started = false
    private val pendingAudio = ArrayDeque<ByteArray>()

    /** Both links are up (device connected, socket open): open the server session. */
    fun start(): List<RelayAction> {
        started = true
        val hello = RelayAction.SendServerText(Hello(sessionId, startSeq).encode())
        val flushed = pendingAudio.map { RelayAction.SendServerBinary(it) }
        pendingAudio.clear()
        return listOf(hello) + flushed
    }

    /**
     * A §C.6 audio packet notified by the device — forward verbatim to the server.
     *
     * Before [start] has run (the socket-open/hello race window), the packet is
     * held in [pendingAudio] and nothing is sent; [start] flushes it after
     * `hello`. After [start], packets are forwarded immediately.
     */
    fun onDeviceAudio(packet: ByteArray): List<RelayAction> =
        if (started) listOf(RelayAction.SendServerBinary(packet))
        else { pendingAudio.addLast(packet); emptyList() }

    /** A §E text frame from the server. */
    fun onServerMessage(text: String): List<RelayAction> =
        when (val msg = parseServerMessage(text)) {
            is ServerMessage.Command -> listOf(forwardCommand(msg))
            is ServerMessage.RequestChunks ->
                listOf(RelayAction.Note("server requested backfill [${msg.start}, ${msg.end}) — not yet supported"))
            is ServerMessage.Transcript ->
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            is ServerMessage.Ack -> emptyList()       // cursor ack; nothing to relay
            is ServerMessage.Proactive -> listOf(forwardProactive(msg))
            is ServerMessage.Unknown -> emptyList()   // forward-compatible: ignore
        }

    /**
     * P3: turn a server-initiated proactive answer into a ChatMessage
     * and hand it to [RelayAction.ForwardToChatHistory] for the runtime
     * to append. The wire payload only carries atom ids; the bubble
     * shows the cited atoms as empty chips (deep-link is the
     * follow-up slice). The request_id becomes the ChatMessage id so
     * later server messages about the same proactive (none today, but
     * the contract is forward-compatible) dedup naturally.
     */
    private fun forwardProactive(msg: ServerMessage.Proactive): RelayAction {
        val chatMessage = ChatMessage(
            id = msg.requestId,
            role = Role.AGENT,
            kind = ChatMessageKind.AGENT_PROACTIVE,
            text = msg.text,
            atoms = msg.atoms.map { atomId ->
                AtomChip(
                    atomId = atomId,
                    sessionId = "",
                    kind = "",
                    text = "",
                    createdAt = "",
                    startMs = 0,
                    score = 0.0,
                )
            },
            traceRequestId = msg.requestId,
        )
        return RelayAction.ForwardToChatHistory(chatMessage)
    }

    /** The device notified a command ack (the command_id bytes) — wrap it as §E. */
    fun onDeviceCommandAck(ackPayload: ByteArray): RelayAction {
        val commandId = String(ackPayload, Charsets.UTF_8)
        return RelayAction.SendServerText(CommandAck(sessionId, commandId).encode())
    }

    /** Tear down: tell the server the session is closing so it flushes. Resets the
     *  hello/audio gate so a reconnect (same instance, re-[start]) drops any audio
     *  held against the dead link and re-arms the hold for the next link. */
    fun stop(): List<RelayAction> {
        started = false
        pendingAudio.clear()
        return listOf(RelayAction.SendServerText(Bye(sessionId).encode()))
    }

    // The device verifies over the raw payload bytes, so the relay reconstructs the
    // on-wire frame: [decoded 64-byte signature][canonical payload JSON bytes].
    private fun forwardCommand(cmd: ServerMessage.Command): RelayAction {
        val signature = Base64.getDecoder().decode(cmd.sig)
        val payload = cmd.payload.toByteArray(Charsets.UTF_8)
        return RelayAction.WriteDeviceCommand(signature + payload)
    }
}

/** Side effects the runtime performs; kept as data so the session stays testable. */
sealed interface RelayAction {
    data class SendServerBinary(val data: ByteArray) : RelayAction
    data class SendServerText(val text: String) : RelayAction
    data class WriteDeviceCommand(val frame: ByteArray) : RelayAction
    /** P3: append the message to the chat history store (process-singleton
     *  on [com.sense.relay.data.RepositoryModule.repos]). The runtime's
     *  RelayService handles this branch. */
    data class ForwardToChatHistory(val message: ChatMessage) : RelayAction
    data class Note(val message: String) : RelayAction
}
