package com.sense.relay.ui.device

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.data.DeviceRepository
import com.sense.relay.data.StatusRepository
import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

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
 */
class DeviceViewModel(
    relayController: RelayController,
    statusRepo: StatusRepository,
    deviceRepo: DeviceRepository,
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
}