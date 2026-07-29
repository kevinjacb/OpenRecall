package com.sense.relay.data

import com.sense.relay.protocol.NameSpeakerMsg
import com.sense.relay.protocol.ReassignSpeakerMsg
import com.sense.relay.protocol.encode
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * The UI's port for sending speaker control messages (name_speaker /
 * reassign_speaker) to the server. The UI never touches the WebSocket
 * directly — [com.sense.relay.RelayService] is the single socket writer.
 *
 * Production wiring is [SpeakerControlPort], a process-singleton queue
 * that RelayService drains while connected. Tests inject a fake that
 * records the calls (see SpeakerActionsTest / VM tests).
 */
interface SpeakerActions {
    fun nameSpeaker(sessionId: String, speakerId: String, name: String)
    fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String = "all")
}

/**
 * Process-singleton buffer of outbound speaker-control JSON frames.
 * [RelayService] polls [drain] while the socket is open and sends each
 * frame via `RelayAction.SendServerText`. Frames produced while the
 * socket is down stay buffered and drain on the next connect (v1: the
 * auto-reconnect path restores the link; a failed send surfaces as an
 * error state on the bubble, not a crash — matches the spec).
 */
object SpeakerControlPort : SpeakerActions {
    private val queue = ConcurrentLinkedQueue<String>()

    override fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        queue.add(NameSpeakerMsg(session_id = sessionId, speaker_id = speakerId, name = name).encode())
    }

    override fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
        queue.add(
            ReassignSpeakerMsg(
                session_id = sessionId,
                from_speaker_id = fromId,
                to_speaker_id = toId,
                scope = scope,
            ).encode()
        )
    }

    /** RelayService calls this only while `socket != null`. Returns one frame, or null if empty. */
    fun poll(): String? = queue.poll()

    fun hasPending(): Boolean = !queue.isEmpty()
}