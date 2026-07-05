package com.sense.relay.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.data.DashboardRepository
import com.sense.relay.data.DashboardState
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn

/**
 * What the Home screen renders. A thin 1:1 projection of [DashboardState]
 * so the screen never imports repository types directly. Sealed for
 * exhaustive `when` rendering.
 */
sealed interface HomeUiState {
    data object Loading : HomeUiState
    data class Loaded(val dashboard: DashboardState.Loaded) : HomeUiState
    data class Failed(val reason: String) : HomeUiState
}

/**
 * Home screen ViewModel. Maps the [DashboardRepository]'s [DashboardState]
 * to a [HomeUiState] and re-hosts it on [viewModelScope] so the screen
 * collects a lifecycle-scoped [StateFlow]. The mapping is total; the
 * ViewModel holds no state of its own beyond the derived flow.
 */
class HomeViewModel(
    repo: DashboardRepository,
) : ViewModel() {

    val state: StateFlow<HomeUiState> = repo.observe()
        .map { it.toUiState() }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HomeUiState.Loading)
}

private fun DashboardState.toUiState(): HomeUiState = when (this) {
    is DashboardState.Loading -> HomeUiState.Loading
    is DashboardState.Loaded -> HomeUiState.Loaded(this)
    is DashboardState.Failed -> HomeUiState.Failed(reason)
}
