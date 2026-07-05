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
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
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
 */
@Composable
fun HomeRoute(modifier: Modifier = Modifier) {
    val vm: HomeViewModel = viewModel(
        factory = viewModelFactory {
            initializer { HomeViewModel(RepositoryModule.repos.dashboard) }
        },
    )
    val state by vm.state.collectAsState()
    HomeScreen(state = state, modifier = modifier)
}

/**
 * Stateless Home content. Renders the three dashboard branches:
 * `Loading` (a centered spinner), `Loaded` (the glanceable dashboard),
 * and `Failed` (a calm empty state). The `Loaded` branch composes the
 * design system's [MetricCard], [ConnectionBadge], [SectionHeader], and
 * [EmptyState] — no bespoke layout primitives.
 */
@Composable
fun HomeScreen(state: HomeUiState, modifier: Modifier = Modifier) {
    when (state) {
        is HomeUiState.Loading -> CenteredSpinner(modifier)
        is HomeUiState.Loaded -> DashboardContent(state.dashboard, modifier)
        is HomeUiState.Failed -> FailedContent(state.reason, modifier)
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
private fun FailedContent(reason: String, modifier: Modifier = Modifier) {
    Box(
        modifier = modifier.fillMaxSize(),
        contentAlignment = Alignment.Center,
    ) {
        EmptyState(
            title = "Can't reach Sense",
            body = reason,
            // The status poller re-polls on its own 2s cadence, so the
            // retry is a no-op affordance for v1 (it reassures rather than
            // triggers). A manual refresh() lands with Phase 5's pull-to-
            // refresh.
            ctaLabel = "Try again",
            onCta = {},
        )
    }
}

@Composable
private fun DashboardContent(
    dashboard: DashboardState.Loaded,
    modifier: Modifier = Modifier,
) {
    Column(
        modifier = modifier
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

        val (serverValue, serverSubtitle) = serverMetric(dashboard.server)
        MetricCard(
            title = "Server",
            value = serverValue,
            subtitle = serverSubtitle,
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
