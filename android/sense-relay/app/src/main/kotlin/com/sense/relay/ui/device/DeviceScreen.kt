package com.sense.relay.ui.device

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.sense.relay.core.model.ApiError
import com.sense.relay.core.result.Outcome
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.RepositoryModule
import com.sense.relay.domain.model.DeviceSummary
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayState
import com.sense.relay.relay.ServerState
import com.sense.relay.ui.design.ConnectionBadge
import com.sense.relay.ui.design.InfoRow
import com.sense.relay.ui.design.SectionHeader
import com.sense.relay.ui.design.Tone

/**
 * Device route. Builds the [DeviceViewModel] from the process singleton
 * (manual DI) and renders the stateless [DeviceScreen].
 */
@Composable
fun DeviceRoute(modifier: Modifier = Modifier) {
    val vm: DeviceViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                DeviceViewModel(
                    relayController = RepositoryModule.repos.relayController,
                    statusRepo = RepositoryModule.repos.status,
                    deviceRepo = RepositoryModule.repos.device,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    DeviceScreen(state = state, modifier = modifier)
}

/**
 * Stateless Device content. A scrollable column of [SectionHeader] +
 * [InfoRow] groups (Device / Relay / Server), topped by a [ConnectionBadge]
 * for the composite state. Diagnostic — no editing, no actions.
 */
@Composable
fun DeviceScreen(state: DeviceUiState, modifier: Modifier = Modifier) {
    Column(
        modifier = modifier
            .fillMaxSize()
            .verticalScroll(rememberScrollState())
            .padding(Spacing.md),
        verticalArrangement = Arrangement.spacedBy(Spacing.md),
    ) {
        ConnectionBadge(state = compositeLabel(state.relay), tone = compositeTone(state.relay))

        SectionHeader(title = "Device")
        DeviceRows(state.relay, state.device)

        SectionHeader(title = "Relay")
        RelayRows(state.relay)

        SectionHeader(title = "Server")
        ServerRows(state.server)
    }
}

@Composable
private fun DeviceRows(relay: RelayState, device: DeviceSummary) {
    val (address, name) = when (val d = relay.device) {
        is DeviceState.Connected -> d.address to (d.name ?: "—")
        else -> "—" to "—"
    }
    InfoRow(label = "BLE address", value = address)
    InfoRow(label = "Name", value = name)
    InfoRow(label = "State", value = deviceStateLabel(relay.device))
    // `lastSeen` lives on the DeviceRepository's DeviceSummary (the relay
    // state itself has no timestamp); null until the first device update.
    InfoRow(label = "Last seen", value = device.lastSeen?.toString() ?: "—")
}

@Composable
private fun RelayRows(relay: RelayState) {
    InfoRow(label = "Connection", value = connectionLabel(relay.connection))
    InfoRow(label = "Session", value = sessionLabel(relay.connection))
    InfoRow(label = "Last error", value = relay.lastError ?: "—")
    InfoRow(label = "Server (relay)", value = serverStateLabel(relay.server))
}

@Composable
private fun ServerRows(server: Outcome<ServerStatus>) {
    when (server) {
        is Outcome.Success -> {
            val s = server.value
            InfoRow(label = "Reachable", value = if (s.reachable) "yes" else "no")
            InfoRow(label = "Authenticated", value = if (s.authenticated) "yes" else "no")
            InfoRow(label = "Version", value = s.version ?: "—")
            InfoRow(label = "Uptime", value = "${s.uptimeSeconds}s")
            InfoRow(label = "Active sessions", value = s.activeSessions.toString())
            InfoRow(label = "Total sessions", value = s.totalSessions.toString())
            InfoRow(label = "Events (24h)", value = s.recentEvents24h.toString())
        }
        is Outcome.Failure -> InfoRow(label = "Server", value = apiErrorLabel(server.error))
    }
}

/**
 * Render the specific [ApiError] variant so the diagnostic screen
 * distinguishes "loading" (the pre-first-poll seed), "unreachable" (DNS/TCP/
 * TLS/timeout), "unauthorized" (401/403 — the "go to Settings" signal), a
 * non-auth HTTP error, and an unknown failure — instead of collapsing them
 * all to "unreachable".
 */
private fun apiErrorLabel(error: ApiError): String = when (error) {
    ApiError.Unauthorized -> "unauthorized"
    is ApiError.Unreachable -> error.reason
    is ApiError.Http -> "server error (${error.code})"
    is ApiError.Unknown -> error.throwable.message ?: "unknown error"
}

private fun compositeLabel(relay: RelayState): String = when (relay.connection) {
    is RelayConnectionState.Live -> "Live"
    RelayConnectionState.Idle -> "Idle"
    RelayConnectionState.BleScanning -> "Scanning"
    is RelayConnectionState.BleConnected -> "Device linked"
    is RelayConnectionState.SocketConnecting -> "Connecting"
    is RelayConnectionState.Reconnecting -> "Reconnecting"
    is RelayConnectionState.Failed -> "Disconnected"
}

private fun compositeTone(relay: RelayState): Tone = when (relay.connection) {
    is RelayConnectionState.Live -> Tone.Accent
    is RelayConnectionState.Failed -> Tone.Muted
    else -> Tone.Neutral
}

private fun deviceStateLabel(d: DeviceState): String = when (d) {
    DeviceState.Unknown -> "Unknown"
    DeviceState.Scanning -> "Scanning"
    is DeviceState.Connected -> "Connected"
    is DeviceState.Disconnected -> "Disconnected (${d.reason})"
}

private fun connectionLabel(c: RelayConnectionState): String = when (c) {
    RelayConnectionState.Idle -> "Idle"
    RelayConnectionState.BleScanning -> "BLE scanning"
    is RelayConnectionState.BleConnected -> "BLE connected (${c.address})"
    is RelayConnectionState.SocketConnecting -> "Opening socket (${c.host})"
    is RelayConnectionState.Live -> "Live"
    is RelayConnectionState.Reconnecting -> "Reconnecting (after ${c.afterMs}ms)"
    is RelayConnectionState.Failed -> "Failed (${c.reason})"
}

private fun sessionLabel(c: RelayConnectionState): String = when (c) {
    is RelayConnectionState.Live -> c.sessionId
    else -> "—"
}

private fun serverStateLabel(s: ServerState): String = when (s) {
    ServerState.Unknown -> "Unknown"
    ServerState.Reachable -> "Reachable"
    ServerState.Authenticated -> "Authenticated"
    is ServerState.Unreachable -> "Unreachable (${s.reason})"
    ServerState.Syncing -> "Syncing"
}