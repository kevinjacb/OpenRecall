package com.openrecall.relay.ui.device

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.core.model.ApiError
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.data.DeviceRepository
import com.openrecall.relay.data.StatusRepository
import com.openrecall.relay.domain.model.DeviceSummary
import com.openrecall.relay.domain.model.ServerStatus
import com.openrecall.relay.relay.RelayController
import com.openrecall.relay.relay.RelayState
import com.openrecall.relay.relay.RelayStarter
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * The Device screen's state: the relay state, the server status, and the
 * device summary (which carries the `lastSeen` timestamp the relay state
 * itself doesn't have) — all carried INDEPENDENTLY. Each is independently
 * loadable, so this is a plain data class (not a sealed wrapper): one may be
 * present while another is "loading" (the server status is a poll that
 * hasn't produced its first result yet, represented as an `Outcome.Failure`
 * with an `Unreachable("loading")` reason until the first poll lands).
 */
data class DeviceUiState(
    val relay: RelayState,
    val server: Outcome<ServerStatus>,
    val device: DeviceSummary,
)

/**
 * Device screen ViewModel. Fans the [RelayController]'s state, the
 * [StatusRepository]'s polled status, and the [DeviceRepository]'s derived
 * summary (for `lastSeen`) into one [DeviceUiState]. The relay + device are
 * always present (hot StateFlows with initial values); the server arrives on
 * the first poll. The seed carries `RelayState.Initial` + an all-null
 * `DeviceSummary` + a "loading" failure so the screen renders before the
 * first poll rather than spinning.
 *
 * Two user actions:
 *  - [onRefresh]: pull-to-refresh — forces an immediate status poll via
 *    [StatusRepository.refresh] and bumps the relay controller's revision so
 *    the reactive state re-emits (defensive — the relay state is live, but a
 *    bump guarantees collectors wake even if the headline state is unchanged).
 *  - [onRetryConnection]: re-launches the foreground [com.openrecall.relay.RelayService]
 *    via [RelayStarter] when the link has dropped — the primary connection
 *    surface (the manual escape hatch; the service also auto-reconnects).
 */
class DeviceViewModel(
    private val relayController: RelayController,
    private val statusRepo: StatusRepository,
    deviceRepo: DeviceRepository,
    private val relayStarter: RelayStarter,
) : ViewModel() {

    val state: StateFlow<DeviceUiState> = combine(
        relayController.state,
        statusRepo.observeStatus(),
        deviceRepo.observeDevice(),
    ) { relay, server, device ->
        DeviceUiState(relay = relay, server = server, device = device)
    }.stateIn(
        viewModelScope,
        SharingStarted.WhileSubscribed(5_000),
        DeviceUiState(
            relay = RelayState.Initial,
            server = Outcome.Failure(ApiError.Unreachable("loading")),
            device = DeviceSummary(address = null, name = null, lastSeen = null),
        ),
    )

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh is in flight; drives the refresh spinner. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    /** Pull-to-refresh: force a status poll + re-emit the relay state. No-op
     *  if a refresh is already in flight. */
    fun onRefresh() {
        if (_isRefreshing.value) return
        _isRefreshing.value = true
        viewModelScope.launch {
            try {
                statusRepo.refresh()
                relayController.requestRefresh()
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