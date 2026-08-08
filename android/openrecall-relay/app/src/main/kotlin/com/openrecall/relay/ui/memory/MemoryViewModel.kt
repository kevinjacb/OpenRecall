package com.openrecall.relay.ui.memory

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.core.util.AUTO_REFRESH_INTERVAL_MS
import com.openrecall.relay.core.util.launchAutoRefresh
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.MemoryOutcome
import com.openrecall.relay.data.MemoryPageOutcome
import com.openrecall.relay.data.MemoryRepository
import com.openrecall.relay.data.StatsOutcome
import com.openrecall.relay.domain.model.MemoryStats
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.flow.update
import kotlinx.coroutines.launch

/**
 * ViewModel for the Memories tab.
 *
 * The screen has two modes, and they are genuinely different queries rather
 * than one with an empty argument:
 *
 * * **Browse** (no query) — a paged, kind-filtered list ordered by
 *   conversation time. This is the default, and before the server grew list
 *   mode the tab could not render at all: `/memory` required a query and
 *   returned only ranked search hits.
 * * **Search** (query present) — semantic retrieval, ranked, unpaged.
 *
 * The kind filter is pushed to the server in browse mode, so a filter finds
 * every matching memory rather than only those already fetched. In search
 * mode the endpoint has no kind parameter, so the filter narrows the returned
 * hits locally — and [MemoryState.filterIsLocal] says which is happening.
 */
class MemoryViewModel(
    private val repo: MemoryRepository,
) : ViewModel() {

    private val _state = MutableStateFlow(MemoryState())
    val state: StateFlow<MemoryState> = _state.asStateFlow()

    /** Drives the pull-to-refresh spinner. */
    val isRefreshing: StateFlow<Boolean> = _state.map { it.loading }.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), false,
    )

    private var pendingQuery: Job? = null

    // The last session loaded via [loadSession] (e.g. a deep-link hop), so
    // [onRefresh] can re-fetch it.
    private var lastSessionId: String? = null

    init {
        loadStats()
        browse()
    }

    /**
     * Update the query. Debounced: a non-empty query searches, an empty one
     * falls back to the browse list — so clearing the box restores the full
     * collection instead of leaving the last results stranded on screen.
     */
    fun onQueryChanged(q: String) {
        _state.update { it.copy(query = q) }
        pendingQuery?.cancel()
        pendingQuery = viewModelScope.launch {
            delay(SEARCH_DEBOUNCE_MS)
            val trimmed = q.trim()
            if (trimmed.isEmpty()) browse() else runSearch(trimmed)
        }
    }

    /**
     * Select a kind filter. In browse mode this re-queries the server, so the
     * filter reaches memories that were never paged in.
     */
    fun onFilterSelected(filter: String) {
        _state.update { it.copy(filter = filter) }
        if (_state.value.lastQuery.isEmpty()) browse()
    }

    fun search() {
        val q = _state.value.query.trim()
        if (q.isEmpty()) browse() else runSearch(q)
    }

    /** Fetch the next page of the browse list. No-op in search mode (which is
     *  unpaged), while loading, or once the cursor runs out. */
    fun onLoadMore() {
        val s = _state.value
        if (s.loading || s.loadingMore) return
        if (s.lastQuery.isNotEmpty()) return
        val cursor = s.nextCursor ?: return
        _state.update { it.copy(loadingMore = true) }
        viewModelScope.launch {
            when (val result = repo.list(kind = s.serverKind, cursor = cursor)) {
                is MemoryPageOutcome.Success -> _state.update {
                    it.copy(
                        loadingMore = false,
                        atoms = it.atoms + result.atoms,
                        nextCursor = result.nextCursor,
                        errorMessage = null,
                    )
                }
                is MemoryPageOutcome.Error -> _state.update {
                    // Keep the rows already on screen; the header stays usable
                    // and a pull-to-refresh retries.
                    it.copy(loadingMore = false, errorMessage = result.message)
                }
            }
        }
    }

    /** Load (or reload) the first page of the browse list. */
    private fun browse() {
        pendingQuery?.cancel()
        pendingQuery = viewModelScope.launch {
            _state.update { it.copy(loading = true, errorMessage = null, lastQuery = "") }
            val kind = _state.value.serverKind
            when (val result = repo.list(kind = kind)) {
                is MemoryPageOutcome.Success -> _state.update {
                    it.copy(
                        loading = false,
                        atoms = result.atoms,
                        nextCursor = result.nextCursor,
                        errorMessage = null,
                    )
                }
                is MemoryPageOutcome.Error -> _state.update {
                    it.copy(loading = false, errorMessage = result.message)
                }
            }
        }
    }

    private fun runSearch(q: String) {
        pendingQuery?.cancel()
        pendingQuery = viewModelScope.launch {
            _state.update { it.copy(loading = true, errorMessage = null) }
            when (val result = repo.search(q)) {
                is MemoryOutcome.Success -> _state.update {
                    it.copy(
                        loading = false,
                        atoms = result.atoms,
                        // Search is unpaged; a cursor here would offer a
                        // "load more" that has nothing to load.
                        nextCursor = null,
                        lastQuery = q,
                        errorMessage = null,
                    )
                }
                is MemoryOutcome.Error -> _state.update {
                    it.copy(loading = false, errorMessage = result.message)
                }
            }
        }
    }

    /** Totals for the header and the kind vocabulary for the chips. A failure
     *  is silent: the counts are an enrichment, and the list still renders. */
    private fun loadStats() {
        viewModelScope.launch {
            when (val result = repo.stats()) {
                is StatsOutcome.Success -> _state.update { it.copy(stats = result.stats) }
                is StatsOutcome.Error -> Unit
            }
        }
    }

    fun loadSession(sessionId: String) {
        lastSessionId = sessionId
        _state.update { it.copy(loading = true, errorMessage = null) }
        viewModelScope.launch {
            when (val result = repo.sessionAtoms(sessionId)) {
                is MemoryOutcome.Success -> _state.update {
                    it.copy(
                        loading = false,
                        atoms = result.atoms,
                        nextCursor = null,
                        errorMessage = null,
                    )
                }
                is MemoryOutcome.Error -> _state.update {
                    it.copy(loading = false, errorMessage = result.message)
                }
            }
        }
    }

    /** Pull-to-refresh: re-run whatever the screen is currently showing, and
     *  re-read the totals (extraction runs in batch, so they move). */
    fun onRefresh() {
        val s = _state.value
        val sessionId = lastSessionId
        loadStats()
        when {
            s.lastQuery.isNotEmpty() -> runSearch(s.lastQuery)
            sessionId != null -> loadSession(sessionId)
            else -> browse()
        }
    }

    private var autoRefreshJob: Job? = null

    /**
     * Start the silent auto-refresh loop (see [AUTO_REFRESH_INTERVAL_MS]), so
     * atoms the extractor writes while the tab is open appear on their own.
     * Idempotent; the route starts it on `ON_RESUME`, stops it on `ON_PAUSE`.
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

    /**
     * One silent tick: re-run whatever the screen is currently showing — the
     * last search, or the deep-linked session's atoms. Unlike [onRefresh] it
     * never sets [MemoryState.loading], because that swaps the list for a
     * spinner; at this cadence the screen would be a strobe. A failed tick is
     * dropped rather than shown: the visible atoms stay, and a persistent
     * problem still surfaces through a user-initiated search or refresh.
     *
     * Nothing runs before the first search — with no query and no session
     * there is nothing to poll (the endpoint has no "list all" mode).
     */
    private suspend fun autoRefreshTick() {
        val s = _state.value
        if (s.loading) return
        // Fall back to the box contents so a tick also retries a search that
        // errored (which never sets `lastQuery`).
        val query = s.lastQuery.ifEmpty { s.query.trim() }
        val sessionId = lastSessionId
        val result = when {
            query.isNotEmpty() -> repo.search(query)
            sessionId != null -> repo.sessionAtoms(sessionId)
            else -> return
        }
        if (result is MemoryOutcome.Success) {
            _state.update {
                it.copy(
                    atoms = result.atoms,
                    lastQuery = if (query.isNotEmpty()) query else it.lastQuery,
                    errorMessage = null,
                )
            }
        }
    }

    override fun onCleared() {
        stopAutoRefresh()
        super.onCleared()
    }

    private companion object {
        const val SEARCH_DEBOUNCE_MS = 250L
    }
}

data class MemoryState(
    val query: String = "",
    val loading: Boolean = false,
    /** True while appending a page; distinct from [loading] so the list stays
     *  on screen instead of being replaced by a spinner. */
    val loadingMore: Boolean = false,
    val atoms: List<MemoryAtom> = emptyList(),
    val nextCursor: String? = null,
    val lastQuery: String = "",
    val errorMessage: String? = null,
    /** The selected kind; [ALL_FILTER] means no filtering. */
    val filter: String = ALL_FILTER,
    /** Totals and the kind vocabulary from `/memory/stats`. */
    val stats: MemoryStats = MemoryStats(),
) {
    /** The `kind` to send to the server, or null for "all kinds". */
    val serverKind: String? get() = filter.takeIf { it != ALL_FILTER }

    /**
     * In search mode the filter cannot be pushed down (the search endpoint
     * has no kind parameter), so it narrows the hits already returned.
     */
    val filterIsLocal: Boolean get() = lastQuery.isNotEmpty() && filter != ALL_FILTER

    /** [atoms] as the list should render them. */
    val visibleAtoms: List<MemoryAtom>
        get() = if (filterIsLocal) {
            atoms.filter { it.kind.equals(filter, ignoreCase = true) }
        } else {
            atoms
        }

    /**
     * The chip row: "All" plus the kinds the extractor has actually produced.
     *
     * Built from the server's `by_kind` rather than hardcoded. The comp's
     * chips (Tasks / People / Decisions / Places / Preferences) are not what
     * the extractor emits, and a hardcoded row would show five filters that
     * match nothing while hiding every kind that exists.
     */
    val filters: List<String> get() = listOf(ALL_FILTER) + stats.kinds

    companion object {
        const val ALL_FILTER = "All"
    }
}
