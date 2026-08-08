package com.opensapien.relay.ui.memory

import com.opensapien.relay.data.MemoryAtom
import com.opensapien.relay.data.MemoryRepository
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
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
 * ViewModel for the Memory browsing screen.
 *
 * Search is debounced (~150ms) to avoid hammering the server on every
 * keystroke; ``lastQuery`` is preserved so the UI can show "last
 * searched: X" when the user clears the box.
 */
class MemoryViewModel(
    private val repo: MemoryRepository,
) : ViewModel() {

    private val _state = MutableStateFlow(MemoryState())
    val state: StateFlow<MemoryState> = _state.asStateFlow()

    /** Drives the pull-to-refresh spinner — true while a search or session
     *  load is in flight (same flag as [MemoryState.loading]). */
    val isRefreshing: StateFlow<Boolean> = _state.map { it.loading }.stateIn(
        viewModelScope, SharingStarted.WhileSubscribed(5_000), false,
    )

    private var pendingSearch: Job? = null
    // The last session loaded via [loadSession] (e.g. a deep-link hop), so
    // [onRefresh] can re-fetch it when no search has been run.
    private var lastSessionId: String? = null

    fun onQueryChanged(q: String) {
        _state.update { it.copy(query = q) }
    }

    fun search() {
        runSearch(_state.value.query.trim())
    }

    /** Run a search for an explicit query (used by [search] from the current
     *  query box and by [onRefresh] to re-run the last search even after the
     *  box has been cleared). No-op on an empty query. */
    private fun runSearch(q: String) {
        if (q.isEmpty()) return
        pendingSearch?.cancel()
        pendingSearch = viewModelScope.launch {
            delay(150)  // debounce
            _state.update { it.copy(loading = true, errorMessage = null) }
            when (val result = repo.search(q)) {
                is com.opensapien.relay.data.MemoryOutcome.Success ->
                    _state.update {
                        it.copy(
                            loading = false,
                            atoms = result.atoms,
                            lastQuery = q,
                            errorMessage = null,
                        )
                    }
                is com.opensapien.relay.data.MemoryOutcome.Error ->
                    _state.update {
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
                is com.opensapien.relay.data.MemoryOutcome.Success ->
                    _state.update {
                        it.copy(loading = false, atoms = result.atoms, errorMessage = null)
                    }
                is com.opensapien.relay.data.MemoryOutcome.Error ->
                    _state.update {
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
        when {
            s.lastQuery.isNotEmpty() -> runSearch(s.lastQuery)
            lastSessionId != null -> loadSession(lastSessionId!!)
            else -> { /* nothing to re-run yet */ }
        }
    }
}

data class MemoryState(
    val query: String = "",
    val loading: Boolean = false,
    val atoms: List<MemoryAtom> = emptyList(),
    val lastQuery: String = "",
    val errorMessage: String? = null,
)
