package com.sense.relay.ui.home

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.sense.relay.core.result.Outcome
import com.sense.relay.core.ui.Spacing
import com.sense.relay.core.util.formatHmMs
import com.sense.relay.data.DashboardState
import com.sense.relay.data.RepositoryModule
import com.sense.relay.domain.model.ServerStatus
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.relay.DeviceState
import com.sense.relay.relay.RelayConnectionState
import com.sense.relay.relay.RelayState
import com.sense.relay.ui.design.ConnectionBadge
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.MetricCard
import com.sense.relay.ui.design.SectionHeader
import com.sense.relay.ui.design.Tone

/**
 * Home route. Constructs the [HomeViewModel] from the process-singleton
 * [RepositoryModule] (manual DI — no Hilt in this slice) and renders the
 * stateless [HomeScreen]. Kept separate from [HomeScreen] so the screen
 * itself is preview-/test-friendly (it takes a plain [HomeUiState]).
 *
 * The two [onOpenDevice] / [onOpenCommands] callbacks are passed in by
 * [com.sense.relay.ui.nav.AppNavigation] and translate to
 * `navController.navigate(Destination.Device.route)` /
 * `navController.navigate(Destination.Commands.route)`. Device and
 * Commands lost their bar slots when the 6-tab bottom bar was
 * collapsed back to 4 tabs (INV-13 compliant); Home is now the
 * drill-down entry point for both admin surfaces.
 */
@Composable
fun HomeRoute(
    onOpenDevice: () -> Unit,
    onOpenCommands: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: HomeViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                HomeViewModel(
                    RepositoryModule.repos.dashboard,
                    RepositoryModule.repos.relayStarter,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    HomeScreen(
        state = state,
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        onRetryConnection = vm::onRetryConnection,
        onOpenDevice = onOpenDevice,
        onOpenCommands = onOpenCommands,
        modifier = modifier,
    )
}

/**
 * Stateless Home content. Renders the three dashboard branches:
 * `Loading` (a centered spinner), `Loaded` (the glanceable dashboard),
 * and `Failed` (a calm empty state). The `Loaded` branch composes the
 * design system's [MetricCard], [ConnectionBadge], [SectionHeader], and
 * [EmptyState] — no bespoke layout primitives.
 *
 * The Loaded branch also surfaces the Device + Commands cards (the
 * drill-down entry points for the two admin surfaces that are not
 * in the bottom bar). The Loaded branch is wrapped in a
 * [PullToRefreshBox] so a pull-down gesture re-fetches status + sessions;
 * a "Retry connection" button appears when the relay link has dropped.
 */
@Composable
fun HomeScreen(
    state: HomeUiState,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onRetryConnection: () -> Unit = {},
    onOpenDevice: () -> Unit = {},
    onOpenCommands: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    when (state) {
        is HomeUiState.Loading -> CenteredSpinner(modifier)
        is HomeUiState.Loaded -> DashboardContent(
            dashboard = state.dashboard,
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            onRetryConnection = onRetryConnection,
            onOpenDevice = onOpenDevice,
            onOpenCommands = onOpenCommands,
            modifier = modifier,
        )
        is HomeUiState.Failed -> FailedContent(
            reason = state.reason,
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = modifier,
        )
    }
}

@Composable
private fun CenteredSpinner(modifier: Modifier = Modifier) {
    Box(
        modifier = modifier.fillMaxSize().padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator()
    }
}

@Composable
private fun FailedContent(
    reason: String,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    Box(
        modifier = modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        EmptyState(
            title = "Can't reach Sense",
            body = reason,
            // Pull-to-refresh / "Try again": forces an immediate status
            // poll + session-list reset instead of waiting for the 2s
            // poll cadence.
            ctaLabel = "Try again",
            onCta = onRefresh,
        )
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun DashboardContent(
    dashboard: DashboardState.Loaded,
    isRefreshing: Boolean,
    onRefresh: () -> Unit,
    onRetryConnection: () -> Unit,
    onOpenDevice: () -> Unit,
    onOpenCommands: () -> Unit,
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
                .padding(Spacing.md),
            verticalArrangement = Arrangement.spacedBy(Spacing.md),
        ) {
            MetricCard(
                title = "Connection",
                value = connectionLabel(dashboard.relay.connection),
            ) {
                val (label, tone) = deviceBadge(dashboard.relay.device)
                ConnectionBadge(state = label, tone = tone)
            }

            // When the relay link has dropped to a recoverable-but-stopped
            // state, surface a "Retry connection" button at the top so the
            // user can re-launch the service without re-running setup.
            if (shouldOfferRetry(dashboard.relay)) {
                RetryConnectionButton(onRetryConnection)
            }

            val (serverValue, serverSubtitle) = serverMetric(dashboard.server)
            MetricCard(
                title = "Server",
                value = serverValue,
                subtitle = serverSubtitle,
            )

            // Drill-down entry points for the two admin surfaces that
            // are not in the bottom bar (Device + Commands). Tapping a
            // card navigates to the corresponding route via the callback
            // the AppNavigation wired in.
            SectionHeader(
                title = "Device & commands",
                modifier = Modifier.padding(top = Spacing.sm),
            )
            DrillDownCard(
                title = "Device",
                subtitle = "BLE link, server status, provisioning",
                onClick = onOpenDevice,
            )
            DrillDownCard(
                title = "Commands",
                subtitle = "Active and recent agent requests",
                onClick = onOpenCommands,
            )

            SectionHeader(
                title = "Recent sessions",
                modifier = Modifier.padding(top = Spacing.sm),
            )
            if (dashboard.recentSessions.isEmpty()) {
                EmptyState(
                    title = "No sessions yet",
                    body = "Recordings from your Sense device show up here.",
                )
            } else {
                dashboard.recentSessions.forEach { session ->
                    SessionRow(session)
                }
            }
        }
    }
}

/**
 * A tappable card for a Home drill-down entry point. Mirrors the
 * existing [SessionRow] visual rhythm (Card + Column + Text) but
 * the entire card is clickable.
 */
@Composable
private fun DrillDownCard(
    title: String,
    subtitle: String,
    onClick: () -> Unit,
) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
        onClick = onClick,
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text(
                text = title,
                style = MaterialTheme.typography.titleMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                text = subtitle,
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun SessionRow(session: SessionSummary) {
    Card(
        modifier = Modifier.fillMaxWidth(),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Text(
                text = session.preview.ifBlank { "(no transcript yet)" },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Text(
                text = formatHmMs(session.durationMs),
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

/** A short, user-facing label for the relay/connection lifecycle. */
private fun connectionLabel(c: RelayConnectionState): String = when (c) {
    RelayConnectionState.Idle -> "Idle"
    RelayConnectionState.BleScanning -> "Scanning"
    is RelayConnectionState.BleConnected -> "Device linked"
    is RelayConnectionState.SocketConnecting -> "Connecting"
    is RelayConnectionState.Live -> "Live"
    is RelayConnectionState.Reconnecting -> "Reconnecting"
    is RelayConnectionState.Failed -> "Disconnected"
}

/**
 * Whether the relay link is in a stopped-but-recoverable state where a
 * manual "Retry connection" is worth offering. Shown when the connection
 * is [RelayConnectionState.Failed] (terminal) or [RelayConnectionState.Idle]
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

/** A full-width "Retry connection" button — the manual escape hatch that
 *  re-launches [com.sense.relay.RelayService] from its last-good config. */
@Composable
private fun RetryConnectionButton(onRetryConnection: () -> Unit) {
    Button(
        onClick = onRetryConnection,
        modifier = Modifier.fillMaxWidth(),
    ) {
        Text("Retry connection")
    }
}

/** The device badge label + tone. `Connected` is the only accent state; the
 *  rest are muted so the dashboard stays calm. */
private fun deviceBadge(d: DeviceState): Pair<String, Tone> = when (d) {
    is DeviceState.Connected -> (d.name ?: d.address) to Tone.Accent
    DeviceState.Scanning -> "Searching…" to Tone.Neutral
    DeviceState.Unknown -> "Not connected" to Tone.Muted
    is DeviceState.Disconnected -> "Disconnected" to Tone.Muted
}

/** The server metric card's big value + subtitle. A [Outcome.Failure]
 *  renders as "Offline" with the reason; success shows the 24h event count
 *  with the version as subtitle. */
private fun serverMetric(server: Outcome<ServerStatus>): Pair<String, String> =
    when (server) {
        is Outcome.Success -> {
            val s = server.value
            val value = if (s.reachable) "${s.recentEvents24h}" else "Offline"
            val subtitle = buildString {
                append(if (s.reachable) "events (24h)" else "unreachable")
                s.version?.let { append(" · v$it") }
            }
            value to subtitle
        }
        is Outcome.Failure -> "Offline" to "unreachable"
    }
