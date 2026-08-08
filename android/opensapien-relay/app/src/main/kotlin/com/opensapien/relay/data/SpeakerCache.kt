package com.opensapien.relay.data

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update
import java.util.concurrent.ConcurrentHashMap

/** A resolved speaker (no biometrics) cached in memory. */
data class SpeakerEntry(
    val speakerId: String,
    val name: String?,
    val isWearer: Boolean,
)

/**
 * In-memory `{speakerId -> SpeakerEntry}` seeded from `GET /speakers` and
 * updated by every transcript §E. The server is the source of truth; v1
 * rebuilds this on app start / after a rename. Thread-safe via a
 * ConcurrentHashMap + a StateFlow mirror for Compose observation.
 */
class SpeakerCache {

    private val entries = ConcurrentHashMap<String, SpeakerEntry>()

    private val _flow = MutableStateFlow<Map<String, SpeakerEntry>>(emptyMap())
    val flow: StateFlow<Map<String, SpeakerEntry>> = _flow.asStateFlow()

    fun get(speakerId: String): SpeakerEntry? = entries[speakerId]

    fun snapshot(): Map<String, SpeakerEntry> = entries.toMap()

    fun upsert(speakerId: String, name: String?, isWearer: Boolean) {
        entries[speakerId] = SpeakerEntry(speakerId, name, isWearer)
        _flow.update { entries.toMap() }
    }

    /** Drop a speaker entry (revert of an optimistic upsert on send failure). */
    fun remove(speakerId: String) {
        // same lock/synchronization as upsert/get — match the file's existing pattern
        entries.remove(speakerId)
        _flow.update { entries.toMap() }
    }

    /** Replace the whole set (from `GET /speakers`). Empty list clears. */
    fun seed(speakers: List<SpeakerEntry>) {
        entries.clear()
        speakers.forEach { entries[it.speakerId] = it }
        _flow.update { entries.toMap() }
    }
}