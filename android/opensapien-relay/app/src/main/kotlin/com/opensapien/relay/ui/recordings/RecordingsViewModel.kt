package com.opensapien.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.model.PagedResult
import com.opensapien.relay.core.ui.toDisplayMessage
import com.opensapien.relay.data.SessionRepository
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.data.httpApiError
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * What the Recordings list renders. Derived from the repository's
 * [PagedResult] + the transient load-errors side-channel; sealed for
 * exhaustive `when` rendering.
 */
sealed interface RecordingsUiState {
    data object Loading : RecordingsUiState
    data class Loaded(
        val items: List<SessionSummary>,
        val canLoadMore: Boolean,
        /** A transient paging error (items already loaded) shown as an inline
         *  "Retry" row, not a full-screen error. `null` when the last load
         *  succeeded. */
        val loadError: ApiError?,
    ) : RecordingsUiState
    data object Empty : RecordingsUiState
    /** Full-screen error — only the initial load (no items yet) failed. */
    data class Error(val reason: String) : RecordingsUiState
}

/**
 * Recordings ViewModel. Observes the paged session list + the transient
 * load-errors flow, and kicks the first page on construction (the list
 * screen owns the initial load — the Home dashboard does not). [onLoadMore]
 * is safe to call repeatedly: it no-ops while a load is in flight or the
 * list can't grow, and the repository itself is idempotent once exhausted.
 */
class RecordingsViewModel(
    private val repo: SessionRepository,
) : ViewModel() {

    val state: StateFlow<RecordingsUiState> = combine(
        repo.observeSessions(),
        repo.observeLoadErrors(),
    ) { paged, loadError ->
        paged.toUiState(loadError)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), RecordingsUiState.Loading)

    // Read/written from viewModelScope (Main) and onLoadMore (Main). Main-
    // only today, but @Volatile guards against a future dispatcher change.
    @Volatile
    private var loading = false

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh is in flight; drives the refresh spinner. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    init {
        loadMore()
    }

    /** Trigger the next page. Called on construction and when the list's last
     *  item is rendered. No-op if a load is already in flight. Also serves as
     *  the "Retry" action for an inline paging error. */
    fun onLoadMore() {
        val s = state.value
        if (s is RecordingsUiState.Loaded && !s.canLoadMore && s.loadError == null) return
        loadMore()
    }

    /**
     * Pull-to-refresh / the "update" option on Recordings: reset to page 1 and
     * re-fetch (the newest sessions land at the top). No-op if a load or a
     * refresh is already in flight — the repository's own [Mutex] serializes
     * them, and the flag prevents overlapping spinners.
     */
    fun onRefresh() {
        if (loading || _isRefreshing.value) return
        _isRefreshing.value = true
        loading = true
        viewModelScope.launch {
            try {
                repo.refreshSessions()
            } finally {
                loading = false
                _isRefreshing.value = false
            }
        }
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

private fun PagedResult<SessionSummary>.toUiState(loadError: ApiError?): RecordingsUiState =
    when (this) {
        is PagedResult.Loading -> RecordingsUiState.Loading
        is PagedResult.Exhausted -> RecordingsUiState.Empty
        is PagedResult.Error -> RecordingsUiState.Error(
            httpApiError(cause).toDisplayMessage(),
        )
        is PagedResult.Page ->
            if (items.isEmpty()) RecordingsUiState.Empty
            else RecordingsUiState.Loaded(
                items = items,
                canLoadMore = nextCursor != null,
                loadError = loadError,
            )
    }