package com.openrecall.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.model.PagedResult
import com.openrecall.relay.core.ui.toDisplayMessage
import com.openrecall.relay.core.util.AUTO_REFRESH_INTERVAL_MS
import com.openrecall.relay.core.util.launchAutoRefresh
import com.openrecall.relay.data.SegmentRepository
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.data.httpApiError
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
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
        val items: List<Segment>,
        val canLoadMore: Boolean,
        /** A transient paging error (items already loaded) shown as an inline
         *  "Retry" row, not a full-screen error. `null` when the last load
         *  succeeded. */
        val loadError: ApiError?,
        /** True when these rows are search hits rather than the browse list.
         *  The empty state and the row layout both differ. */
        val searching: Boolean,
    ) : RecordingsUiState
    data object Empty : RecordingsUiState
    /** Full-screen error — only the initial load (no items yet) failed. */
    data class Error(val reason: String) : RecordingsUiState
}

/**
 * Recordings ViewModel. Observes the paged segment list + the transient
 * load-errors flow, and kicks the first page on construction (the list screen
 * owns the initial load — the Home dashboard does not). [onLoadMore] is safe
 * to call repeatedly: it no-ops while a load is in flight or the list can't
 * grow, and the repository itself is idempotent once exhausted.
 *
 * **Search runs on the server.** The old screen filtered the pages already
 * loaded, which meant a query silently missed every recording the user hadn't
 * scrolled to — the failure mode where search appears to work and quietly
 * doesn't. `GET /segments?q=` scans the transcripts instead, so a hit from
 * three weeks ago surfaces without paging to it. That makes the query a
 * network call, hence the debounce.
 */
class RecordingsViewModel(
    private val repo: SegmentRepository,
) : ViewModel() {

    private val _query = MutableStateFlow("")
    /** The search box's current text. */
    val query: StateFlow<String> = _query.asStateFlow()

    /** The query the *list* currently reflects, which lags [_query] by the
     *  debounce. Kept separate so the empty state names what was searched. */
    private val appliedQuery = MutableStateFlow("")

    val state: StateFlow<RecordingsUiState> = combine(
        repo.observeSegments(),
        repo.observeLoadErrors(),
        appliedQuery,
    ) { paged, loadError, applied ->
        paged.toUiState(loadError, applied.isNotBlank())
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), RecordingsUiState.Loading)

    private var pendingQuery: Job? = null

    /**
     * Update the search text and schedule the fetch. Debounced so typing
     * issues one request after the user pauses rather than one per keystroke;
     * clearing the box is applied on the same path, which returns the list to
     * browse mode.
     */
    fun onQueryChange(value: String) {
        _query.value = value
        pendingQuery?.cancel()
        pendingQuery = viewModelScope.launch {
            delay(SEARCH_DEBOUNCE_MS)
            val q = value.trim()
            appliedQuery.value = q
            repo.setQuery(q)
        }
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
     * Pull-to-refresh: reset to page 1 and re-fetch (the newest recordings
     * land at the top), keeping any active query. No-op if a load or a
     * refresh is already in flight — the repository's own mutex serializes
     * them, and the flag prevents overlapping spinners.
     */
    fun onRefresh() {
        if (loading || _isRefreshing.value) return
        _isRefreshing.value = true
        loading = true
        viewModelScope.launch {
            try {
                repo.refresh()
            } finally {
                loading = false
                _isRefreshing.value = false
            }
        }
    }

    private var autoRefreshJob: Job? = null

    /**
     * Start the silent auto-refresh loop (see [AUTO_REFRESH_INTERVAL_MS]).
     * Idempotent — a second call while the loop is running is a no-op. The
     * route starts this on `ON_RESUME` and stops it on `ON_PAUSE`, so the
     * polling is foreground- and screen-scoped.
     */
    fun startAutoRefresh(intervalMs: Long = AUTO_REFRESH_INTERVAL_MS) {
        if (autoRefreshJob?.isActive == true) return
        autoRefreshJob = viewModelScope.launchAutoRefresh(intervalMs) { autoRefreshTick() }
    }

    /** Stop the auto-refresh loop. Safe to call when it isn't running. */
    fun stopAutoRefresh() {
        autoRefreshJob?.cancel()
        autoRefreshJob = null
    }

    /** One silent tick: merge the newest page in. Skipped while a page load
     *  or a pull-to-refresh is in flight, so ticks never queue up behind a
     *  slow request or fight a user gesture. */
    private suspend fun autoRefreshTick() {
        if (loading || _isRefreshing.value) return
        loading = true
        try {
            repo.refresh(silent = true)
        } finally {
            loading = false
        }
    }

    override fun onCleared() {
        stopAutoRefresh()
        super.onCleared()
    }

    private fun loadMore() {
        if (loading) return
        loading = true
        viewModelScope.launch {
            try {
                repo.loadMore()
            } finally {
                loading = false
            }
        }
    }

    private companion object {
        const val SEARCH_DEBOUNCE_MS = 250L
    }
}

/** Project a page into UI state. */
private fun PagedResult<Segment>.toUiState(
    loadError: ApiError?,
    searching: Boolean,
): RecordingsUiState = when (this) {
    is PagedResult.Loading -> RecordingsUiState.Loading
    is PagedResult.Exhausted -> RecordingsUiState.Empty
    is PagedResult.Error -> RecordingsUiState.Error(httpApiError(cause).toDisplayMessage())
    is PagedResult.Page ->
        if (items.isEmpty()) {
            RecordingsUiState.Empty
        } else {
            RecordingsUiState.Loaded(
                items = items,
                canLoadMore = nextCursor != null,
                loadError = loadError,
                searching = searching,
            )
        }
}
