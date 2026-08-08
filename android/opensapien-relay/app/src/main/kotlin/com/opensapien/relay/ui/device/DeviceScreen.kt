package com.opensapien.relay.ui.device

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ColumnScope
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.opensapien.relay.core.model.ApiError
import com.opensapien.relay.core.result.Outcome
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.domain.model.DeviceSummary
import com.opensapien.relay.domain.model.ServerStatus
import com.opensapien.relay.relay.DeviceState
import com.opensapien.relay.relay.RelayConnectionState
import com.opensapien.relay.relay.RelayState
import com.opensapien.relay.relay.ServerState
import com.opensapien.relay.ui.copyToClipboard
import com.opensapien.relay.ui.design.SecondaryButton
import com.opensapien.relay.ui.design.SenseCard
import com.opensapien.relay.ui.design.SenseDetailHeader
import com.opensapien.relay.ui.design.SenseGroupLabel
import com.opensapien.relay.ui.design.SenseIcons
import com.opensapien.relay.ui.design.SettingsGroup
import com.opensapien.relay.ui.design.SettingsValueRow
import com.opensapien.relay.ui.design.StatTile
import com.opensapien.relay.ui.design.StatusBadge
import com.opensapien.relay.ui.design.StatusTone

/**
 * Device route. Builds the [DeviceViewModel] from the process singleton
 * (manual DI) and renders the stateless [DeviceScreen] under a back-arrow
 * detail header — Device is a push screen reached from Settings, not a bar
 * tab.
 */
@Composable
fun DeviceRoute(onBack: () -> Unit, modifier: Modifier = Modifier) {
    val vm: DeviceViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                DeviceViewModel(
                    relayController = RepositoryModule.repos.relayController,
                    statusRepo = RepositoryModule.repos.status,
                    deviceRepo = RepositoryModule.repos.device,
                    relayStarter = RepositoryModule.repos.relayStarter,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    Column(
        modifier
            .fillMaxSize()
            .background(SenseTheme.colors.canvas)
            .statusBarsPadding(),
    ) {
        SenseDetailHeader(title = "Device", onBack = onBack)
        DeviceScreen(
            state = state,
            isRefreshing = isRefreshing,
            onRefresh = vm::onRefresh,
            onRetryConnection = vm::onRetryConnection,
        )
    }
}

/**
 * Stateless Device content, in the comp's Settings vocabulary: a link hero
 * card carrying the composite status, then [SenseGroupLabel] + [SettingsGroup]
 * blocks for Device / Relay / Server.
 *
 * Diagnostic — no editing, but a "Retry connection" button when the link has
 * dropped, and a pull-to-refresh that forces a status poll plus a relay state
 * re-emit.
 *
 * The comp has no design for this screen (Diagnostics is an app-side addition
 * to Settings), so the layout is extrapolated from the vocabulary the comp
 * does define rather than invented: the same grouped white cards, hairline
 * dividers and group labels used by Settings, plus the Home screen's
 * [StatTile] for the two server counters worth reading at a glance.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun DeviceScreen(
    state: DeviceUiState,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onRetryConnection: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = modifier.fillMaxSize(),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .padding(horizontal = 20.dp)
                .padding(bottom = 28.dp),
        ) {
            LinkHero(relay = state.relay, onRetryConnection = onRetryConnection)

            SenseGroupLabel("Device", Modifier.padding(top = 26.dp, bottom = 12.dp))
            SettingsGroup { DeviceRows(state.relay, state.device) }

            SenseGroupLabel("Relay", Modifier.padding(top = 26.dp, bottom = 12.dp))
            SettingsGroup { RelayRows(state.relay) }

            SenseGroupLabel("Server", Modifier.padding(top = 26.dp, bottom = 12.dp))
            ServerSection(state.server)
        }
    }
}

/**
 * The hero: composite status pill, the device name, and the long-form
 * connection detail underneath. This is the one place the verbose
 * [connectionLabel] earns its length — Home flattens the same state into a
 * single badge, so the reason a socket is retrying is only readable here.
 */
@Composable
private fun LinkHero(relay: RelayState, onRetryConnection: () -> Unit) {
    val colors = SenseTheme.colors
    SenseCard {
        StatusBadge(label = compositeLabel(relay), tone = compositeTone(relay))
        Text(
            text = (relay.device as? DeviceState.Connected)?.name ?: "OpenSapien",
            style = MaterialTheme.typography.headlineSmall,
            color = colors.ink,
            modifier = Modifier.padding(top = 12.dp),
        )
        Text(
            text = connectionLabel(relay.connection),
            style = MaterialTheme.typography.bodySmall,
            color = colors.grey,
            modifier = Modifier.padding(top = 4.dp),
        )
        if (shouldOfferRetry(relay)) {
            SecondaryButton(
                label = "Retry connection",
                onClick = onRetryConnection,
                modifier = Modifier.padding(top = 14.dp),
            )
        }
    }
}

/**
 * Whether the relay link is stopped-but-recoverable, where a manual "Retry
 * connection" is worth offering. Shown when the connection is
 * [RelayConnectionState.Failed] (terminal) or [RelayConnectionState.Idle]
 * (never started / stopped itself), OR when the BLE device is
 * [DeviceState.Disconnected]. Active states (Scanning/Connecting/Live/
 * Reconnecting) hide it — the system is already trying.
 */
private fun shouldOfferRetry(relay: RelayState): Boolean =
    when (relay.connection) {
        is RelayConnectionState.Failed, RelayConnectionState.Idle -> true
        is RelayConnectionState.Reconnecting -> false
        else -> relay.device is DeviceState.Disconnected
    }

@Composable
private fun ColumnScope.DeviceRows(relay: RelayState, device: DeviceSummary) {
    val context = LocalContext.current
    val (address, name) = when (val d = relay.device) {
        is DeviceState.Connected -> d.address to (d.name ?: "—")
        else -> "—" to "—"
    }
    SettingsValueRow(
        label = "BLE address",
        value = address,
        onClick = address.takeIf { it != "—" }?.let {
            { copyToClipboard(context, "BLE address", it) }
        },
    )
    SettingsValueRow(label = "Name", value = name)
    SettingsValueRow(label = "State", value = deviceStateLabel(relay.device))
    // `lastSeen` lives on the DeviceRepository's DeviceSummary (the relay
    // state itself has no timestamp); null until the first device update.
    SettingsValueRow(
        label = "Last seen",
        value = device.lastSeen?.toString() ?: "—",
        last = true,
    )
}

@Composable
private fun ColumnScope.RelayRows(relay: RelayState) {
    SettingsValueRow(label = "Connection", value = connectionLabel(relay.connection))
    SettingsValueRow(label = "Session", value = sessionLabel(relay.connection))
    SettingsValueRow(label = "Last error", value = relay.lastError ?: "—")
    SettingsValueRow(
        label = "Server",
        value = serverStateLabel(relay.server),
        last = true,
    )
}

/**
 * The polled `/status` block. On success the two counters that read well at a
 * glance go in [StatTile]s and the rest stay as rows; on failure the whole
 * group collapses to one card naming the specific [ApiError].
 */
@Composable
private fun ServerSection(server: Outcome<ServerStatus>) {
    val colors = SenseTheme.colors
    when (server) {
        is Outcome.Success -> {
            val s = server.value
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.spacedBy(12.dp),
            ) {
                StatTile(
                    label = "Active sessions",
                    value = s.activeSessions.toString(),
                    icon = SenseIcons.Sparkle,
                    modifier = Modifier.weight(1f),
                )
                StatTile(
                    label = "Events (24h)",
                    value = s.recentEvents24h.toString(),
                    icon = SenseIcons.Signal,
                    modifier = Modifier.weight(1f),
                )
            }
            SettingsGroup(Modifier.padding(top = 12.dp)) {
                SettingsValueRow(label = "Reachable", value = if (s.reachable) "Yes" else "No")
                SettingsValueRow(
                    label = "Authenticated",
                    value = if (s.authenticated) "Yes" else "No",
                )
                SettingsValueRow(label = "Version", value = s.version ?: "—")
                SettingsValueRow(label = "Uptime", value = formatUptime(s.uptimeSeconds))
                SettingsValueRow(
                    label = "Total sessions",
                    value = s.totalSessions.toString(),
                    last = true,
                )
            }
        }

        is Outcome.Failure -> SenseCard {
            Text(
                text = "Status unavailable",
                style = MaterialTheme.typography.titleSmall,
                color = colors.ink,
            )
            Text(
                text = apiErrorLabel(server.error),
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 4.dp),
            )
        }
    }
}

/**
 * Uptime as a human duration rather than a raw second count — the comp writes
 * every duration this way. Largest two units only: `3d 4h`, `2h 51m`, `14m
 * 08s`, `47s`.
 */
internal fun formatUptime(seconds: Long): String {
    if (seconds < 0) return "—"
    val d = seconds / 86_400
    val h = (seconds % 86_400) / 3_600
    val m = (seconds % 3_600) / 60
    val s = seconds % 60
    return when {
        d > 0 -> "${d}d ${h}h"
        h > 0 -> "${h}h ${m}m"
        m > 0 -> "${m}m ${"%02d".format(s)}s"
        else -> "${s}s"
    }
}

/**
 * Render the specific [ApiError] variant so the diagnostic screen
 * distinguishes "loading" (the pre-first-poll seed), "unreachable" (DNS/TCP/
 * TLS/timeout), "unauthorized" (401/403 — the "go to Settings" signal), a
 * non-auth HTTP error, and an unknown failure — instead of collapsing them
 * all to "unreachable".
 *
 * This INTENTIONALLY diverges from [com.opensapien.relay.core.ui.toDisplayMessage]
 * (the consumer-facing `ErrorMapper`): the Device screen is diagnostic, so it
 * shows the curated short reason (`Unreachable.reason`, `Http.code`) rather
 * than the generic "Can't reach the server". The `Unknown` branch does NOT
 * surface `throwable.message` (no raw exception text in the UI) — a stable
 * "unknown error" label stands in.
 */
private fun apiErrorLabel(error: ApiError): String = when (error) {
    ApiError.Unauthorized -> "unauthorized"
    is ApiError.Unreachable -> error.reason
    is ApiError.Http -> "server error (${error.code})"
    is ApiError.Unknown -> "unknown error"
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

/**
 * Composite state → pill tone. Live is the only healthy state; anything
 * mid-handshake is [StatusTone.Working] so the pill's dot breathes and the
 * screen reads as "still trying" without a spinner.
 */
private fun compositeTone(relay: RelayState): StatusTone = when (relay.connection) {
    is RelayConnectionState.Live -> StatusTone.Healthy
    RelayConnectionState.Idle -> StatusTone.Idle
    is RelayConnectionState.Failed -> StatusTone.Problem
    RelayConnectionState.BleScanning,
    is RelayConnectionState.BleConnected,
    is RelayConnectionState.SocketConnecting,
    is RelayConnectionState.Reconnecting,
    -> StatusTone.Working
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
