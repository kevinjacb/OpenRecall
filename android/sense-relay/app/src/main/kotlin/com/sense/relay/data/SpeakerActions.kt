package com.sense.relay.data

import com.sense.relay.protocol.NameSpeakerMsg
import com.sense.relay.protocol.ReassignSpeakerMsg
import com.sense.relay.protocol.encode
import kotlinx.coroutines.CoroutineDispatcher
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import java.util.concurrent.ConcurrentLinkedQueue

/**
 * The UI's port for renaming/reassigning speakers. Production wiring is
 * [HttpSpeakerActions] (HTTP-always — works for offline/historical sessions).
 * Tests inject a fake that records the calls.
 */
interface SpeakerActions {
    suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String)
    suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String = "all")
}

/**
 * HTTP-backed [SpeakerActions]. Delegates to [SpeakerRepository]; `sessionId`
 * is accepted for interface parity but ignored (v1 reassign is global).
 */
class HttpSpeakerActions(
    private val repo: SpeakerRepository,
    private val io: CoroutineDispatcher = Dispatchers.IO,
) : SpeakerActions {
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        withContext(io) { repo.renameSpeaker(speakerId, name) }
    }
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) =
        withContext(io) { repo.reassignSpeaker(fromId, toId, scope) }
}

/** No-op default so VM tests that don't care about speaker actions can omit the arg. */
object NoopSpeakerActions : SpeakerActions {
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {}
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {}
}

/** Legacy WS queue — removed in Task 5; kept compiling here by implementing the suspend iface. */
object SpeakerControlPort : SpeakerActions {
    private val queue = ConcurrentLinkedQueue<String>()
    override suspend fun nameSpeaker(sessionId: String, speakerId: String, name: String) {
        queue.add(NameSpeakerMsg(session_id = sessionId, speaker_id = speakerId, name = name).encode())
    }
    override suspend fun reassignSpeaker(sessionId: String, fromId: String, toId: String, scope: String) {
        queue.add(ReassignSpeakerMsg(session_id = sessionId, from_speaker_id = fromId, to_speaker_id = toId, scope = scope).encode())
    }
    fun poll(): String? = queue.poll()
    fun hasPending(): Boolean = !queue.isEmpty()
}