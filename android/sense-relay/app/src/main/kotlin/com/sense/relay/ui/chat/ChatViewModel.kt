package com.sense.relay.ui.chat

import com.sense.relay.data.AgentOutcome
import com.sense.relay.data.AgentRepository
import com.sense.relay.data.AtomChip
import com.sense.relay.data.ChatHistoryStore
import com.sense.relay.data.ChatMessage
import com.sense.relay.data.Role
import com.sense.relay.core.SenseLog
import com.sense.relay.core.TraceContext
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
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
    private val sessionId: String? = null,
    savedState: SavedStateHandle = SavedStateHandle(),
) : ViewModel() {

    private val _draft = MutableStateFlow(savedState.get<String>(KEY_DRAFT) ?: "")
    val draft: StateFlow<String> = _draft.asStateFlow()

    private val _loading = MutableStateFlow(false)
    val loading: StateFlow<Boolean> = _loading.asStateFlow()

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
                val outcome = repo.ask(sessionId, text)
                when (outcome) {
                    is AgentOutcome.Answer -> {
                        val agent = ChatMessage(
                            id = "msg-${System.currentTimeMillis()}-agent",
                            role = Role.AGENT,
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
            } finally {
                _loading.value = false
            }
        }
    }

    fun clear() {
        store.clear()
        _draft.value = ""
    }

    companion object {
        private const val KEY_DRAFT = "chat_draft"
    }
}
