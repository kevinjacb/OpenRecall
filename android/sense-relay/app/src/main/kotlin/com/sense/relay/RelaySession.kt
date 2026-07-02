package com.sense.relay

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

    /** Both links are up (device connected, socket open): open the server session. */
    fun start(): List<RelayAction> =
        listOf(RelayAction.SendServerText(Hello(sessionId, startSeq).encode()))

    /** A §C.6 audio packet notified by the device — forward verbatim to the server. */
    fun onDeviceAudio(packet: ByteArray): RelayAction =
        RelayAction.SendServerBinary(packet)

    /** A §E text frame from the server. */
    fun onServerMessage(text: String): List<RelayAction> =
        when (val msg = parseServerMessage(text)) {
            is ServerMessage.Command -> listOf(forwardCommand(msg))
            is ServerMessage.RequestChunks ->
                listOf(RelayAction.Note("server requested backfill [${msg.start}, ${msg.end}) — not yet supported"))
            is ServerMessage.Transcript ->
                listOf(RelayAction.Note("transcript: ${msg.text}"))
            is ServerMessage.Ack -> emptyList()       // cursor ack; nothing to relay
            is ServerMessage.Unknown -> emptyList()   // forward-compatible: ignore
        }

    /** The device notified a command ack (the command_id bytes) — wrap it as §E. */
    fun onDeviceCommandAck(ackPayload: ByteArray): RelayAction {
        val commandId = String(ackPayload, Charsets.UTF_8)
        return RelayAction.SendServerText(CommandAck(sessionId, commandId).encode())
    }

    /** Tear down: tell the server the session is closing so it flushes. */
    fun stop(): List<RelayAction> =
        listOf(RelayAction.SendServerText(Bye(sessionId).encode()))

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
    data class Note(val message: String) : RelayAction
}
