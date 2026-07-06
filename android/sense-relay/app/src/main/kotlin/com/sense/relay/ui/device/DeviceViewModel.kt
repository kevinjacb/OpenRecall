package com.sense.relay.ui.device

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.data.StatusRepository
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.relay.RelayController
import com.sense.relay.relay.RelayState
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.stateIn

/**
 * The Device screen's state: the relay state and the server status carried
 * INDEPENDENTLY. Both are independently loadable — the relay is a hot
 * StateFlow with a value from the moment the controller exists, while the
 * server status is a poll that hasn't produced its first result yet. So
 * this is a plain data class (not a sealed wrapper): one may be present
 * while the other is "loading" (represented as an `Outcome.Failure` with an
 * `Unreachable("loading")` reason until the first poll lands).
 */
data class DeviceUiState(
    val relay: RelayState,
    val server: Outcome<ServerStatus>,
)

/**
 * Device screen ViewModel. Fans the [RelayController]'s state and the
 * [StatusRepository]'s polled status into one [DeviceUiState]. The relay
 * is always present (the controller is a process-singleton with an Initial
 * value); the server arrives on the first poll. The seed carries
 * `RelayState.Initial` + a "loading" failure so the screen renders before
 * the first poll rather than spinning.
 */
class DeviceViewModel(
    relayController: RelayController,
    statusRepo: StatusRepository,
) : ViewModel() {

    val state: StateFlow<DeviceUiState> = combine(
        relayController.state,
        statusRepo.observeStatus(),
    ) { relay, server ->
        DeviceUiState(relay = relay, server = server)
    }.stateIn(
        viewModelScope,
        SharingStarted.WhileSubscribed(5_000),
        DeviceUiState(
            relay = RelayState.Initial,
            server = Outcome.Failure(ApiError.Unreachable("loading")),
        ),
    )
}