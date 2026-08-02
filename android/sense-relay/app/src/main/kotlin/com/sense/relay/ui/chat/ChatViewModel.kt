package com.sense.relay.ui.chat

import com.sense.relay.data.AgentOutcome
import com.sense.relay.data.AgentRepository
import com.sense.relay.data.AtomChip
import com.sense.relay.data.ChatHistoryStore
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.ChatMessageKind
import com.sense.relay.data.Role
import com.sense.relay.core.SenseLog
import com.sense.relay.core.TraceContext
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayController
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.launch

/**
 * ViewModel for the Chat screen. Holds the draft text in
 * [SavedStateHandle] (process-death survival), owns one chat session
 * in [ChatHistoryStore], and delegates the actual ask to
 * [AgentRepository].
 *
 * **Trace propagation (N4.1):** every ``ask()`` writes ONE log line
 * carrying the request_id, retrieval_trace_id, and audit_id from the
 * AgentOutcome so an operator can grep one log file and follow a
 * single chat from user to backend to audit.
 */
class ChatViewModel(
    private val repo: AgentRepository,
    private val store: ChatHistoryStore,
    private val relayController: RelayController = RelayController,
    private val speakerActions: com.sense.relay.data.SpeakerActions = com.sense.relay.data.NoopSpeakerActions,
    savedState: SavedStateHandle = SavedStateHandle(),
) : ViewModel() {

    private val _draft = MutableStateFlow(savedState.get<String>(KEY_DRAFT) ?: "")
    val draft: StateFlow<String> = _draft.asStateFlow()

    /**
     * Passthrough to [ChatHistoryStore.messages]. The screen collects
     * this and dispatches each message to the right bubble Composable
     * based on [ChatMessage.kind]. Added for the ChatScreen Composable
     * (P2-answers user-facing surface). The store is the source of
     * truth; this property is a one-line accessor.
     */
    val messages: StateFlow<List<ChatMessage>> = store.messages

    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()

    /**
     * Transient error surfaced when a nudge-driven rename (the
     * [com.sense.relay.data.ChatMessageKind.NAME_SPEAKER] bubble) fails to
     * reach the server. The old WS control-frame path buffered silently; HTTP
     * cannot buffer across restarts, so a send failure is surfaced here and
     * shown as a Snackbar in [ChatScreen] (and cleared via
     * [dismissSpeakerError] once the snackbar dismisses). Null when idle.
     */
    private val _speakerError = MutableStateFlow<String?>(null)
    val speakerError: StateFlow<String?> = _speakerError.asStateFlow()

    fun dismissSpeakerError() {
        _speakerError.value = null
    }

    /**
     * The live relay session id published app-wide by [RelayService] on
     * socket open (`RelayConnectionState.Live(sessionId)`), read
     * imperatively at call time. A `StateFlow.value` read is always
     * current — no `stateIn`/subscription needed, avoiding the
     * WhileSubscribed "nobody subscribes → stays null" trap. Returns
     * null when the relay is not live, so [nameSpeaker] and [ask]
     * gracefully no-op / send a server-less ask instead of dropping the
     * request with a stale id.
     */
    private fun currentSessionId(): String? =
        (relayController.state.value.connection as? RelayConnectionState.Live)?.sessionId

    init {
        // Persist draft across process death.
        viewModelScope.launch {
            _draft.collect { savedState[KEY_DRAFT] = it }
        }
    }

    fun onTextChanged(value: String) {
        _draft.value = value
    }

    fun ask() {
        val text = _draft.value.trim()
        if (text.isEmpty()) return
        val userId = "msg-${System.currentTimeMillis()}-${(0..9999).random()}"
        val pending = ChatMessage(
            id = userId,
            role = Role.USER,
            text = text,
        )
        store.append(pending)
        _draft.value = ""
        _loading.value = true
        viewModelScope.launch {
            try {
                val outcome = repo.ask(currentSessionId(), text)
                when (outcome) {
                    is AgentOutcome.Answer -> {
                        val agent = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-agent",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_ANSWER,
                            text = outcome.text,
                            atoms = outcome.atoms.map {
                                com.sense.relay.data.AtomChip(
                                    atomId = it.atomId,
                                    sessionId = it.sessionId,
                                    kind = it.kind,
                                    text = it.text,
                                    createdAt = it.createdAt,
                                    startMs = it.startMs,
                                    score = it.score,
                                )
                            },
                            traceRequestId = outcome.trace.requestId,
                            traceRetrievalId = outcome.trace.retrievalTraceId ?: "",
                            traceAuditId = outcome.trace.auditId ?: "",
                        )
                        store.append(agent)
                        SenseLog.d(
                            tag = "ChatViewModel",
                            msg = "agent answer",
                            trace = outcome.trace,
                        )
                    }
                    is AgentOutcome.Refuse -> {
                        val msg = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-refuse",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_REFUSE,
                            text = outcome.reason.replace("_", " ").replaceFirstChar { it.uppercase() },
                            traceRequestId = outcome.trace.requestId,
                            traceRetrievalId = outcome.trace.retrievalTraceId ?: "",
                            traceAuditId = outcome.trace.auditId ?: "",
                        )
                        store.append(msg)
                        SenseLog.d(
                            tag = "ChatViewModel",
                            msg = "agent refuse",
                            trace = outcome.trace,
                        )
                    }
                    is AgentOutcome.Error -> {
                        val msg = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-error",
                            role = Role.AGENT,
                            kind = ChatMessageKind.AGENT_ERROR,
                            text = "Error: ${outcome.message}",
                            traceRequestId = outcome.trace.requestId,
                        )
                        store.append(msg)
                        SenseLog.w(
                            tag = "ChatViewModel",
                            msg = "agent error",
                            trace = outcome.trace,
                        )
                    }
                }
            } catch (e: CancellationException) {
                throw e
            } catch (e: Exception) {
                // Defense-in-depth: the repository maps every wire error to
                // AgentOutcome.Error, but a programming error in the mapping
                // (or anywhere in this block) must never crash the app — show
                // an error bubble and log the throwable so the bug is still
                // discoverable instead of killing the process.
                store.append(
                    ChatMessage(
                        id = "msg-${System.currentTimeMillis()}-error",
                        role = Role.AGENT,
                        kind = ChatMessageKind.AGENT_ERROR,
                        text = "Error: ${e.message ?: e.javaClass.simpleName}",
                    )
                )
                SenseLog.e(
                    tag = "ChatViewModel",
                    msg = "agent ask crashed: ${e.javaClass.simpleName}",
                    t = e,
                )
            } finally {
                _loading.value = false
            }
        }
    }

    fun clear() {
        store.clear()
        _draft.value = ""
    }

    /**
     * Name (or rename) a speaker from a [com.sense.relay.data.ChatMessageKind.NAME_SPEAKER]
     * nudge. Calls the HTTP rename endpoint; the server's `set_display_name`
     * applies, and the next transcript §E carries the new `speaker_name`.
     * Optimistically collapses the nudge bubble into a plain proactive line so
     * the user sees their choice reflected immediately. Send failure does NOT
     * crash: it is logged and surfaced via [speakerError] (shown as a Snackbar
     * in [ChatScreen]) so the user knows the server didn't record the name.
     */
    fun nameSpeaker(speakerId: String, name: String) {
        val sid = currentSessionId()
        if (sid.isNullOrEmpty()) return
        viewModelScope.launch {
            runCatching { speakerActions.nameSpeaker(sid, speakerId, name) }
                .onSuccess { _speakerError.value = null }
                .onFailure {
                    SenseLog.e(tag = "ChatViewModel", msg = "name_speaker send failed: ${it.javaClass.simpleName}", t = it)
                    _speakerError.value = "Couldn't save the name on the server"
                }
        }
    }

    companion object {
        private const val KEY_DRAFT = "chat_draft"
    }
}
