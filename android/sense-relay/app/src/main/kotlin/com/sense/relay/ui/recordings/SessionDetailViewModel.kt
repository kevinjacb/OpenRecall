package com.sense.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.result.Outcome
import com.sense.relay.core.ui.toDisplayMessage
import com.sense.relay.data.SpeakerActions
import com.sense.relay.data.SpeakerCache
import com.sense.relay.data.SpeakerControlPort
import com.sense.relay.data.SessionRepository
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.onStart
import kotlinx.coroutines.flow.stateIn

/**
 * What the SessionDetail screen renders. Progressive: the summary lands
 * first ([LoadedSummary]), then the event timeline fills in ([Loaded]).
 * Sealed for exhaustive `when` rendering.
 */
sealed interface SessionDetailUiState {
    data object Loading : SessionDetailUiState
    data class LoadedSummary(val summary: SessionSummary) : SessionDetailUiState
    data class Loaded(
        val summary: SessionSummary,
        val events: List<CaptureEvent>,
    ) : SessionDetailUiState
    data class Failed(val reason: String) : SessionDetailUiState
}

/**
 * SessionDetail ViewModel. `combine`s the summary fetch with the event-list
 * fetch, but seeds the events flow with a `null` via [onStart] so the state
 * can advance to [SessionDetailUiState.LoadedSummary] the moment the summary
 * arrives — without waiting for the (potentially larger) event list. When
 * the events arrive it advances to [SessionDetailUiState.Loaded].
 *
 * Error policy: a summary failure is [SessionDetailUiState.Failed] (nothing
 * to show); an events failure keeps [SessionDetailUiState.LoadedSummary] (the
 * summary still renders, the timeline just stays empty).
 *
 * Refresh: the underlying per-session flows are one-shot cold flows (each
 * `GET` emits once). [onRefresh] bumps a [revision] trigger that
 * [flatMapLatest] uses to cancel the in-flight collection and re-collect the
 * two flows from scratch — re-fetching summary + events. The [isRefreshing]
 * flag is cleared on the new collection's first emission (summary landed) so
 * the pull-to-refresh spinner dismisses as soon as the refresh produces a
 * new state.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SessionDetailViewModel(
    private val id: SessionId,
    private val repo: SessionRepository,
    val speakerCache: SpeakerCache = SpeakerCache(),
    private val speakerActions: SpeakerActions = SpeakerControlPort,
) : ViewModel() {

    // Bumped by [onRefresh] to re-collect the one-shot per-session flows.
    private val revision = MutableStateFlow(0)

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh re-fetch is in flight. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    val state: StateFlow<SessionDetailUiState> = revision
        .flatMapLatest {
            // Flow is covariant, so a non-null events flow binds to a nullable-
            // typed one; `onStart { emit(null) }` then seeds a "pending" tick so
            // `combine` fires as soon as the summary lands (events still null
            // → LoadedSummary). The explicit nullable element type MUST be on
            // the bare flow before `onStart` — inlining drops the annotation,
            // `onStart` infers a non-null element, and `emit(null)` won't compile.
            val eventsOrPending: Flow<Outcome<List<CaptureEvent>>?> = repo.observeSessionEvents(id)
            combine(
                repo.observeSession(id),
                eventsOrPending.onStart { emit(null) },
            ) { summary, events -> reduce(summary, events) }
                // Clear the refresh flag on the first emission of this (re-)
                // collection — the summary fetch has landed, the spinner can
                // dismiss. A no-op for the initial collection (flag is false).
                .onEach { if (_isRefreshing.value) _isRefreshing.value = false }
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SessionDetailUiState.Loading)

    /**
     * Pull-to-refresh: re-fetch the session summary + event timeline. Bumps
     * [revision] so [flatMapLatest] cancels the current collection and re-
     * collects the one-shot flows from scratch. No-op if a refresh is already
     * in flight.
     */
    fun onRefresh() {
        if (_isRefreshing.value) return
        _isRefreshing.value = true
        revision.value = revision.value + 1
    }

    /**
     * Name (or rename) a speaker from the Recordings timeline. Sends the
     * `name_speaker` control message; the server's `set_display_name` applies,
     * and the next transcript §E / `/speakers` refresh carries the new name.
     * Optimistically upserts the cache so the label updates immediately.
     * Send failure does NOT crash (the port buffers; RelayService drains).
     */
    fun renameSpeaker(speakerId: String, name: String) {
        val isWearer = speakerCache.get(speakerId)?.isWearer ?: false
        speakerCache.upsert(speakerId, name, isWearer)
        runCatching { speakerActions.nameSpeaker(id.value, speakerId, name) }
    }

    /**
     * Reassign a speaker's utterances to another known speaker. v1 uses
     * `scope="all"` (the server is session-scoped, so "all of this speaker" is
     * "this conversation"). Labels refresh on the next transcript / refresh.
     */
    fun reassignSpeaker(fromId: String, toId: String) {
        runCatching { speakerActions.reassignSpeaker(id.value, fromId, toId) }
    }
}

private fun reduce(
    summary: Outcome<SessionDetails>,
    events: Outcome<List<CaptureEvent>>?,
): SessionDetailUiState = when (summary) {
    is Outcome.Failure -> SessionDetailUiState.Failed(summary.error.toDisplayMessage())
    is Outcome.Success -> {
        val s = summary.value.summary
        when (events) {
            null -> SessionDetailUiState.LoadedSummary(s) // events still pending
            is Outcome.Failure -> SessionDetailUiState.LoadedSummary(s) // events failed; summary stands
            is Outcome.Success -> SessionDetailUiState.Loaded(s, events.value)
        }
    }
}
