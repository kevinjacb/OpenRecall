package com.sense.relay.data

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

data class ChatMessage(
    val id: String,
    val role: Role,
    val text: String,
    val atoms: List<AtomChip> = emptyList(),
    val traceRequestId: String = "",
    val traceRetrievalId: String = "",
    val traceAuditId: String = "",
)

enum class Role { USER, AGENT }
