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

    private val _query = MutableStateFlow("")
    /** The search box's current text. Drives the filter in [state]. */
    val query: StateFlow<String> = _query.asStateFlow()

    val state: StateFlow<RecordingsUiState> = combine(
        repo.observeSessions(),
        repo.observeLoadErrors(),
        _query,
    ) { paged, loadError, query ->
        paged.toUiState(loadError, query)
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), RecordingsUiState.Loading)

    /** Update the search text. Filtering is local and immediate — no
     *  debounce is needed because nothing is fetched. */
    fun onQueryChange(value: String) {
        _query.value = value
    }

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

/**
 * Project a page into UI state, applying the [query] filter.
 *
 * The filter is client-side over the pages already loaded: the sessions API
 * takes only `limit` and `cursor`, with no search parameter, so there is
 * nothing to push down to the server. A query therefore searches what has
 * been paged in so far — scrolling further widens it. `canLoadMore` is left
 * as the page reports it, so paging keeps working while filtered.
 */
private fun PagedResult<SessionSummary>.toUiState(
    loadError: ApiError?,
    query: String,
): RecordingsUiState = when (this) {
    is PagedResult.Loading -> RecordingsUiState.Loading
    is PagedResult.Exhausted -> RecordingsUiState.Empty
    is PagedResult.Error -> RecordingsUiState.Error(httpApiError(cause).toDisplayMessage())
    is PagedResult.Page -> {
        val matches = items.filter { it.matches(query) }
        if (matches.isEmpty()) {
            RecordingsUiState.Empty
        } else {
            RecordingsUiState.Loaded(
                items = matches,
                canLoadMore = nextCursor != null,
                loadError = loadError,
            )
        }
    }
}

private fun SessionSummary.matches(query: String): Boolean {
    val q = query.trim()
    if (q.isEmpty()) return true
    return preview.contains(q, ignoreCase = true) || id.value.contains(q, ignoreCase = true)
}