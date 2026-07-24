package com.sense.relay.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.data.DashboardRepository
import com.sense.relay.data.DashboardState
import com.sense.relay.relay.RelayStarter
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

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
 * collects a lifecycle-scoped [StateFlow]. The mapping is total.
 *
 * Two user actions live here:
 *  - [onRefresh]: a pull-to-refresh gesture — forces an immediate status
 *    poll + a reset-to-page-1 of the session list via [DashboardRepository.refresh],
 *    toggling [isRefreshing] around the suspend call so the screen can show
 *    the [androidx.compose.material3.pulltorefresh.PullToRefreshBox] spinner.
 *  - [onRetryConnection]: re-launches the foreground [com.sense.relay.RelayService]
 *    via [RelayStarter] when the link has dropped (the manual escape hatch;
 *    [com.sense.relay.RelayService] also auto-reconnects with backoff).
 */
class HomeViewModel(
    private val repo: DashboardRepository,
    private val relayStarter: RelayStarter,
) : ViewModel() {

    val state: StateFlow<HomeUiState> = repo.observe()
        .map { it.toUiState() }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HomeUiState.Loading)

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh is in flight; drives the refresh spinner. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    /** Pull-to-refresh: re-fetch status + reset sessions to page 1. Guards
     *  against overlapping refreshes so a second gesture while one is in
     *  flight is a no-op (the spinner is already showing). */
    fun onRefresh() {
        if (_isRefreshing.value) return
        viewModelScope.launch {
            _isRefreshing.value = true
            try {
                repo.refresh()
            } finally {
                _isRefreshing.value = false
            }
        }
    }

    /** "Retry connection": re-launch the relay service from its last-good
     *  config. No-op guard is the service's own `onStartCommand`. */
    fun onRetryConnection() {
        relayStarter.start()
    }
}

private fun DashboardState.toUiState(): HomeUiState = when (this) {
    is DashboardState.Loading -> HomeUiState.Loading
    is DashboardState.Loaded -> HomeUiState.Loaded(this)
    is DashboardState.Failed -> HomeUiState.Failed(reason)
}
