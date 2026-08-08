package com.openrecall.relay.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.sync.Mutex
import kotlinx.coroutines.sync.withLock

/**
 * In-memory chat history store with a [flush] seam (M8).
 *
 * The VM holds one instance; messages survive recomposition + process
 * death (in the next slice, a DataStore-backed impl persists across
 * app restarts). [flush] is called from
 * [androidx.lifecycle.ProcessLifecycleOwner] so unsent messages are
 * persisted before the OS reclaims the process.
 */
class ChatHistoryStore {

    private val _messages = MutableStateFlow<List<ChatMessage>>(emptyList())
    val messages: StateFlow<List<ChatMessage>> = _messages.asStateFlow()

    private val mutex = Mutex()
    private var unflushedDelta: Int = 0

    fun append(msg: ChatMessage) {
        _messages.update { it + msg }
        unflushedDelta += 1
    }

    fun replace(pending: ChatMessage, final: ChatMessage) {
        _messages.update { list ->
            list.map { if (it.id == pending.id) final else it }
        }
        unflushedDelta += 1
    }

    fun clear() {
        _messages.value = emptyList()
        unflushedDelta = 0
    }

    /**
     * Called from the process-lifecycle observer. Persists any
     * unsent messages (a no-op in this in-memory impl; the future
     * DataStore-backed version writes to disk).
     */
    suspend fun flush() = mutex.withLock {
        // No-op for the in-memory store. The contract — "flush is
        // called, unflushedDelta resets" — is preserved.
        unflushedDelta = 0
    }

    fun unflushedCount(): Int = unflushedDelta
}

/**
 * Variant of a [ChatMessage]. The screen's [com.openrecall.relay.ui.chat.ChatMessageList]
 * dispatches on this with an exhaustive `when` — adding a new
 * variant is a compile error in the screen, which is the right
 * failure mode.
 *
 * - USER_TEXT: a message the user typed.
 * - AGENT_ANSWER: a Return or ReturnWithUncertainty outcome from the
 *   agent. Carries [ChatMessage.atoms] (the cited memory atoms).
 * - AGENT_REFUSE: a Refuse outcome (no supporting memory). The
 *   screen renders this as the friendly-copy + "browse memory"
 *   link variant.
 * - AGENT_ERROR: an Error outcome (network failure, server error,
 *   rate limit, etc.). The text is pre-mapped via
 *   [com.openrecall.relay.core.ui.toDisplayMessage] in the route.
 * - AGENT_PROACTIVE: a server-initiated message (P3 proactive
 *   trigger). The server pushed an unsolicited answer based on
 *   the user's recent activity; the user did not ask a question.
 *   The chat screen renders a "Proactive" tag so the user knows
 *   the message wasn't a response to them.
 */
enum class ChatMessageKind { USER_TEXT, AGENT_ANSWER, AGENT_REFUSE, AGENT_ERROR, AGENT_PROACTIVE, NAME_SPEAKER }

data class ChatMessage(
    val id: String,
    val role: Role,
    /**
     * The variant of this message. Defaults to [ChatMessageKind.USER_TEXT]
     * so the user-typed construction site in
     * [com.openrecall.relay.ui.chat.ChatViewModel.ask] doesn't need to
     * set it explicitly. The three agent-outcome branches set the
     * appropriate AGENT_* value.
     */
    val kind: ChatMessageKind = ChatMessageKind.USER_TEXT,
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
    /** NAME_SPEAKER only: the parsed propose payload (which speaker to name). */
    val propose: com.openrecall.relay.protocol.NameSpeakerPropose? = null,
    /** NAME_SPEAKER only: the session id to address the name_speaker control to. */
    val sessionId: String? = null,
    /** NAME_SPEAKER only: convenience copy of [propose]'s speakerId (or a reassign target). */
    val speakerId: String? = null,
)

enum class Role { USER, AGENT }
