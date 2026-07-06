package com.sense.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.result.Outcome
import com.sense.relay.core.ui.toDisplayMessage
import com.sense.relay.data.SessionRepository
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
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
 */
class SessionDetailViewModel(
    id: SessionId,
    repo: SessionRepository,
) : ViewModel() {

    // Flow is covariant, so a non-null events flow binds to a nullable-typed
    // one; `onStart { emit(null) }` then seeds a "pending" tick so `combine`
    // fires as soon as the summary lands (events still null → LoadedSummary).
    private val eventsOrPending: Flow<Outcome<List<CaptureEvent>>?> =
        repo.observeSessionEvents(id)

    val state: StateFlow<SessionDetailUiState> = combine(
        repo.observeSession(id),
        eventsOrPending.onStart { emit(null) },
    ) { summary, events ->
        reduce(summary, events)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SessionDetailUiState.Loading)
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
