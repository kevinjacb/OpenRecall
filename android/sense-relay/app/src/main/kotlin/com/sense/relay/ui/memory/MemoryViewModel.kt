package com.sense.relay.ui.memory

import com.sense.relay.data.MemoryAtom
import com.sense.relay.data.MemoryRepository
import androidx.lifecycle.SavedStateHandle
import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
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

    private var pendingSearch: Job? = null

    fun onQueryChanged(q: String) {
        _state.update { it.copy(query = q) }
    }

    fun search() {
        val q = _state.value.query.trim()
        if (q.isEmpty()) return
        pendingSearch?.cancel()
        pendingSearch = viewModelScope.launch {
            delay(150)  // debounce
            _state.update { it.copy(loading = true, errorMessage = null) }
            when (val result = repo.search(q)) {
                is com.sense.relay.data.MemoryOutcome.Success ->
                    _state.update {
                        it.copy(
                            loading = false,
                            atoms = result.atoms,
                            lastQuery = q,
                            errorMessage = null,
                        )
                    }
                is com.sense.relay.data.MemoryOutcome.Error ->
                    _state.update {
                        it.copy(loading = false, errorMessage = result.message)
                    }
            }
        }
    }

    fun loadSession(sessionId: String) {
        _state.update { it.copy(loading = true, errorMessage = null) }
        viewModelScope.launch {
            when (val result = repo.sessionAtoms(sessionId)) {
                is com.sense.relay.data.MemoryOutcome.Success ->
                    _state.update {
                        it.copy(loading = false, atoms = result.atoms, errorMessage = null)
                    }
                is com.sense.relay.data.MemoryOutcome.Error ->
                    _state.update {
                        it.copy(loading = false, errorMessage = result.message)
                    }
            }
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
