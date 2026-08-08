package com.openrecall.relay.ui.home

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.data.ConfigurationRepository
import com.openrecall.relay.data.DashboardRepository
import com.openrecall.relay.data.DashboardState
import com.openrecall.relay.data.RelaySettingsRepository
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.relay.RelayStarter
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * What the Home screen renders. A thin projection of [DashboardState] plus
 * the one piece of config Home needs: whether a device has ever been
 * provisioned.
 *
 * That flag matters because Home is now the launch screen. Before the
 * redesign the setup wizard was the launcher, so Home could assume a
 * provisioned device; now a first-run user lands here and must be shown a
 * calm "not connected / set up" state rather than an error.
 */
sealed interface HomeUiState {
    data object Loading : HomeUiState

    data class Loaded(
        val dashboard: DashboardState.Loaded,
        /** False until the setup wizard has successfully paired a device. */
        val provisioned: Boolean,
    ) : HomeUiState

    data class Failed(val reason: String) : HomeUiState
}

/**
 * Home screen ViewModel. Combines the [DashboardRepository]'s aggregate with
 * the persisted provisioning flag and re-hosts the result on
 * [viewModelScope]. The mapping is total.
 *
 * Three user actions:
 *  - [onRefresh]: pull-to-refresh — forces a status poll, resets the
 *    recordings list to page 1 and re-reads the device status, toggling
 *    [isRefreshing] around the call.
 *  - [onRetryConnection]: re-launches the foreground
 *    [com.openrecall.relay.RelayService] via [RelayStarter] when the link has
 *    dropped. The service also auto-reconnects; this is the manual hatch.
 */
class HomeViewModel(
    private val repo: DashboardRepository,
    private val relayStarter: RelayStarter,
    configuration: ConfigurationRepository,
    /** Optional so tests can omit it; the battery tile simply reads "—". */
    private val relaySettings: RelaySettingsRepository? = null,
) : ViewModel() {

    val state: StateFlow<HomeUiState> = combine(
        repo.observe(),
        configuration.observe(),
    ) { dashboard, config ->
        when (dashboard) {
            is DashboardState.Loading -> HomeUiState.Loading
            is DashboardState.Loaded -> HomeUiState.Loaded(dashboard, config.provisioned)
            is DashboardState.Failed -> HomeUiState.Failed(dashboard.reason)
        }
    }.stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), HomeUiState.Loading)

    private val _device = MutableStateFlow<DeviceStatus?>(null)
    /**
     * What the relay knows about the wearable. Null until the first fetch
     * lands or when it fails — the tiles fall back to "—" rather than to a
     * number nobody measured.
     */
    val device: StateFlow<DeviceStatus?> = _device.asStateFlow()

    init {
        loadDeviceStatus()
    }

    private fun loadDeviceStatus() {
        val repo = relaySettings ?: return
        viewModelScope.launch {
            _device.value = (repo.deviceStatus() as? Outcome.Success)?.value
        }
    }

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh is in flight; drives the refresh spinner. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    /** Pull-to-refresh: re-fetch status + reset recordings to page 1. Guards
     *  against overlapping refreshes so a second gesture mid-flight is a
     *  no-op — the spinner is already showing. */
    fun onRefresh() {
        if (_isRefreshing.value) return
        viewModelScope.launch {
            _isRefreshing.value = true
            try {
                repo.refresh()
                loadDeviceStatus()
            } finally {
                _isRefreshing.value = false
            }
        }
    }

    /** "Retry connection": re-launch the relay service from its last-good
     *  config. The service's own `onStartCommand` is the no-op guard. */
    fun onRetryConnection() {
        relayStarter.start()
    }
}
