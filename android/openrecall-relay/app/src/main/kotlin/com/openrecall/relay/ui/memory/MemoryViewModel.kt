package com.openrecall.relay.ui.memory

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.MemoryOutcome
import com.openrecall.relay.data.MemoryRepository
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
 * Search is debounced (~150ms) so typing doesn't hammer the server;
 * [MemoryState.lastQuery] is preserved so the UI can still say what was
 * searched after the box is cleared.
 */
class MemoryViewModel(
    private val repo: MemoryRepository,
) : ViewModel() {

    private val _state = MutableStateFlow(MemoryState())
    val state: StateFlow<MemoryState> = _state.asStateFlow()

    /** Drives the pull-to-refresh spinner — true while a search or session
     *  load is in flight (the same flag as [MemoryState.loading]). */
    val isRefreshing: StateFlow<Boolean> = _state.map { it.loading }.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), false,
    )

    private var pendingSearch: Job? = null

    // The last session loaded via [loadSession] (e.g. a deep-link hop), so
    // [onRefresh] can re-fetch it when no search has been run.
    private var lastSessionId: String? = null

    /**
     * Update the query and kick a search. [runSearch]'s debounce cancels the
     * previous pending job, so typing issues one request after the user
     * pauses rather than one per keystroke — which is what makes
     * search-as-you-type affordable here.
     */
    fun onQueryChanged(q: String) {
        _state.update { it.copy(query = q) }
        runSearch(q.trim())
    }

    /**
     * Select a kind filter.
     *
     * Filtering happens locally over the atoms already returned: the
     * `/memory` search endpoint takes a query, an optional session id and a
     * limit — it has no `kind` parameter — so there is nothing to push down
     * to the server.
     */
    fun onFilterSelected(filter: String) {
        _state.update { it.copy(filter = filter) }
    }

    fun search() {
        runSearch(_state.value.query.trim())
    }

    /** Run a search for an explicit query — used by [search] for the current
     *  box contents and by [onRefresh] to re-run the last search even after
     *  the box has been cleared. No-op on an empty query. */
    private fun runSearch(q: String) {
        if (q.isEmpty()) return
        pendingSearch?.cancel()
        pendingSearch = viewModelScope.launch {
            delay(SEARCH_DEBOUNCE_MS)
            _state.update { it.copy(loading = true, errorMessage = null) }
            when (val result = repo.search(q)) {
                is MemoryOutcome.Success -> _state.update {
                    it.copy(
                        loading = false,
                        atoms = result.atoms,
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

    fun loadSession(sessionId: String) {
        lastSessionId = sessionId
        _state.update { it.copy(loading = true, errorMessage = null) }
        viewModelScope.launch {
            when (val result = repo.sessionAtoms(sessionId)) {
                is MemoryOutcome.Success -> _state.update {
                    it.copy(loading = false, atoms = result.atoms, errorMessage = null)
                }
                is MemoryOutcome.Error -> _state.update {
                    it.copy(loading = false, errorMessage = result.message)
                }
            }
        }
    }

    /**
     * Pull-to-refresh: re-run the last action. If a search was run, re-search
     * the last query (even if the box has since been cleared); else if a
     * session was loaded (deep-link), re-load it; else no-op.
     */
    fun onRefresh() {
        val s = _state.value
        val sessionId = lastSessionId
        when {
            s.lastQuery.isNotEmpty() -> runSearch(s.lastQuery)
            sessionId != null -> loadSession(sessionId)
            else -> Unit // nothing to re-run yet
        }
    }

    private companion object {
        const val SEARCH_DEBOUNCE_MS = 150L
    }
}

data class MemoryState(
    val query: String = "",
    val loading: Boolean = false,
    val atoms: List<MemoryAtom> = emptyList(),
    val lastQuery: String = "",
    val errorMessage: String? = null,
    /** The selected kind filter; [ALL_FILTER] means no filtering. */
    val filter: String = ALL_FILTER,
) {
    /** [atoms] narrowed by [filter] — what the list actually renders. */
    val visibleAtoms: List<MemoryAtom>
        get() = if (filter == ALL_FILTER) {
            atoms
        } else {
            atoms.filter { it.kind.equals(filter, ignoreCase = true) }
        }

    companion object {
        const val ALL_FILTER = "All"

        /**
         * The filter row, taken from the comp.
         *
         * **Partly a placeholder.** These are the atom kinds the comp shows;
         * the server's extractor decides what `kind` values it actually
         * emits, and there is no endpoint listing them. A filter whose kind
         * the extractor never produces will simply match nothing.
         */
        val FILTERS = listOf(ALL_FILTER, "Task", "Person", "Decision", "Place", "Preference")
    }
}
