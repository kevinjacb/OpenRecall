package com.sense.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.model.PagedResult
import com.sense.relay.data.SessionRepository
import com.sense.relay.domain.model.SessionSummary
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * What the Recordings list renders. Derived from the repository's
 * [PagedResult]; sealed for exhaustive `when` rendering.
 */
sealed interface RecordingsUiState {
    data object Loading : RecordingsUiState
    data class Loaded(
        val items: List<SessionSummary>,
        val canLoadMore: Boolean,
    ) : RecordingsUiState
    data object Empty : RecordingsUiState
    data class Error(val reason: String) : RecordingsUiState
}

/**
 * Recordings ViewModel. Observes the paged session list and kicks the first
 * page on construction (the list screen owns the initial load — the Home
 * dashboard does not). [onLoadMore] is safe to call repeatedly: it no-ops
 * while a load is in flight or the list can't grow, and the repository
 * itself is idempotent once exhausted.
 */
class RecordingsViewModel(
    private val repo: SessionRepository,
) : ViewModel() {

    val state: StateFlow<RecordingsUiState> = repo.observeSessions()
        .map { it.toUiState() }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), RecordingsUiState.Loading)

    private var loading = false

    init {
        loadMore()
    }

    /** Trigger the next page. Called on construction and when the list's last
     *  item is rendered. No-op if a load is already in flight. */
    fun onLoadMore() {
        val s = state.value
        if (s is RecordingsUiState.Loaded && !s.canLoadMore) return
        loadMore()
    }

    private fun loadMore() {
        if (loading) return
        loading = true
        viewModelScope.launch {
            try {
                repo.loadMoreSessions()
            } finally {
                loading = false
            }
        }
    }
}

private fun PagedResult<SessionSummary>.toUiState(): RecordingsUiState = when (this) {
    is PagedResult.Loading -> RecordingsUiState.Loading
    is PagedResult.Exhausted -> RecordingsUiState.Empty
    is PagedResult.Error -> RecordingsUiState.Error(cause.message ?: "Couldn't load recordings")
    is PagedResult.Page ->
        if (items.isEmpty()) RecordingsUiState.Empty
        else RecordingsUiState.Loaded(items = items, canLoadMore = nextCursor != null)
}
