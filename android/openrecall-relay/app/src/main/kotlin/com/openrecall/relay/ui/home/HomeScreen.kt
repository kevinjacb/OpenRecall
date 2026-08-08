package com.openrecall.relay.ui.home

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.core.util.formatHmMs
import com.openrecall.relay.core.util.formatRelative
import com.openrecall.relay.data.DashboardState
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.domain.model.DeviceStatus
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.ServerStatus
import com.openrecall.relay.relay.DeviceState
import com.openrecall.relay.relay.RelayConnectionState
import com.openrecall.relay.relay.RelayState
import com.openrecall.relay.ui.design.DeviceVisual
import com.openrecall.relay.ui.design.DeviceVisualState
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.LiveBars
import com.openrecall.relay.ui.design.PlaceholderTag
import com.openrecall.relay.ui.design.PrimaryButton
import com.openrecall.relay.ui.design.SecondaryButton
import com.openrecall.relay.ui.design.SectionHeader
import com.openrecall.relay.ui.design.SectionHeaderAction
import com.openrecall.relay.ui.design.RecallAppHeader
import com.openrecall.relay.ui.design.RecallCard
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.StatTile
import com.openrecall.relay.ui.design.StatusBadge
import com.openrecall.relay.ui.design.StatusDot
import com.openrecall.relay.ui.design.StatusTone
import com.openrecall.relay.ui.design.TileMeter
import com.openrecall.relay.ui.design.TileSegments

/**
 * Home route. Builds the [HomeViewModel] from the process-singleton
 * [RepositoryModule] (manual DI — no Hilt in this project) and renders the
 * stateless [HomeScreen], which takes a plain [HomeUiState] so it stays
 * preview- and test-friendly.
 *
 * Home is the app's launch screen. [onSetUpDevice] opens the pairing wizard;
 * [onOpenSegment] pushes a recording detail; [onSeeAllRecordings] and
 * [onOpenSettings] switch tabs.
 */
@Composable
fun HomeRoute(
    onSetUpDevice: () -> Unit,
    onOpenSettings: () -> Unit,
    onSeeAllRecordings: () -> Unit,
    onOpenSegment: (SegmentId) -> Unit,
    modifier: Modifier = Modifier,
) {
    val vm: HomeViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                HomeViewModel(
                    RepositoryModule.repos.dashboard,
                    RepositoryModule.repos.relayStarter,
                    RepositoryModule.repos.configuration,
                    RepositoryModule.repos.relaySettings,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    val device by vm.device.collectAsState()
    HomeScreen(
        state = state,
        device = device,
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        onRetryConnection = vm::onRetryConnection,
        onSetUpDevice = onSetUpDevice,
        onOpenSettings = onOpenSettings,
        onSeeAllRecordings = onSeeAllRecordings,
        onOpenSegment = onOpenSegment,
        modifier = modifier,
    )
}

/**
 * Stateless Home content — the comp's dashboard: brand header, the device
 * hero with its status, two stat tiles, the live-capture card, the relay
 * server row, and the most recent sessions.
 */
@Composable
fun HomeScreen(
    state: HomeUiState,
    device: DeviceStatus? = null,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onRetryConnection: () -> Unit = {},
    onSetUpDevice: () -> Unit = {},
    onOpenSettings: () -> Unit = {},
    onSeeAllRecordings: () -> Unit = {},
    onOpenSegment: (SegmentId) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    Box(modifier.fillMaxSize().background(colors.canvas)) {
        when (state) {
            is HomeUiState.Loading -> Box(
                Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) { CircularProgressIndicator(color = colors.accent) }

            is HomeUiState.Failed -> Box(
                Modifier.fillMaxSize(),
                contentAlignment = Alignment.Center,
            ) {
                EmptyState(
                    title = "Can't reach your relay",
                    body = state.reason,
                    ctaLabel = "Try again",
                    onCta = onRefresh,
                )
            }

            is HomeUiState.Loaded -> Dashboard(
                dashboard = state.dashboard,
                device = device,
                provisioned = state.provisioned,
                isRefreshing = isRefreshing,
                onRefresh = onRefresh,
                onRetryConnection = onRetryConnection,
                onSetUpDevice = onSetUpDevice,
                onOpenSettings = onOpenSettings,
                onSeeAllRecordings = onSeeAllRecordings,
                onOpenSegment = onOpenSegment,
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Dashboard(
    dashboard: DashboardState.Loaded,
    device: DeviceStatus?,
    provisioned: Boolean,
    isRefreshing: Boolean,
    onRefresh: () -> Unit,
    onRetryConnection: () -> Unit,
    onSetUpDevice: () -> Unit,
    onOpenSettings: () -> Unit,
    onSeeAllRecordings: () -> Unit,
    onOpenSegment: (SegmentId) -> Unit,
) {
    val colors = RecallTheme.colors
    val relay = dashboard.relay
    val status = deviceStatus(relay, provisioned)

    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        Column(
            modifier = Modifier
                .fillMaxSize()
                .verticalScroll(rememberScrollState())
                .statusBarsPadding()
                .padding(horizontal = 20.dp)
                .padding(bottom = 28.dp),
        ) {
            RecallAppHeader(title = "OpenRecall Relay", onSettings = onOpenSettings)

            DeviceVisual(
                state = status.visual,
                modifier = Modifier.padding(top = 2.dp),
            )

            Column(
                modifier = Modifier.fillMaxWidth(),
                horizontalAlignment = Alignment.CenterHorizontally,
            ) {
                Text(
                    text = deviceName(relay.device),
                    style = MaterialTheme.typography.headlineMedium,
                    color = colors.ink,
                )
                StatusBadge(
                    label = status.label,
                    tone = status.tone,
                    modifier = Modifier.padding(top = 8.dp),
                )
            }

            // First run: the wizard is no longer the launcher, so Home owns
            // the call to action that gets a device paired.
            if (!provisioned) {
                PrimaryButton(
                    label = "Set up your OpenRecall",
                    onClick = onSetUpDevice,
                    modifier = Modifier.padding(top = 22.dp),
                )
            } else if (status.canRetry) {
                SecondaryButton(
                    label = "Retry connection",
                    onClick = onRetryConnection,
                    modifier = Modifier.padding(top = 22.dp),
                )
            }

            Row(
                modifier = Modifier.fillMaxWidth().padding(top = 22.dp),
                horizontalArrangement = Arrangement.spacedBy(10.dp),
            ) {
                BatteryTile(device, Modifier.weight(1f))
                BleLinkTile(device = relay.device, modifier = Modifier.weight(1f))
            }

            HearingNowCard(
                relay = relay,
                lastRecording = dashboard.recentSegments.firstOrNull(),
                modifier = Modifier.padding(top = 10.dp),
            )

            RelayServerCard(
                server = dashboard.server,
                modifier = Modifier.padding(top = 10.dp),
            )

            SectionHeader(
                title = "Recent recordings",
                modifier = Modifier.padding(top = 28.dp),
                trailing = { SectionHeaderAction("See all", onSeeAllRecordings) },
            )

            if (dashboard.recentSegments.isEmpty()) {
                RecallCard(modifier = Modifier.padding(top = 12.dp)) {
                    Text(
                        text = "Nothing recorded yet. Recordings land here as soon as " +
                            "your OpenRecall starts hearing.",
                        style = MaterialTheme.typography.bodyMedium,
                        color = colors.grey,
                        textAlign = TextAlign.Center,
                        modifier = Modifier.fillMaxWidth().padding(vertical = 12.dp),
                    )
                }
            } else {
                dashboard.recentSegments.forEach { segment ->
                    SegmentCard(
                        segment = segment,
                        onClick = { onOpenSegment(segment.id) },
                        modifier = Modifier.padding(top = 10.dp),
                    )
                }
            }
        }
    }
}

/**
 * Battery level.
 *
 * The relay reports a value, but until the wearable actually gains battery
 * sensing that value is a fixed server-side placeholder — the status payload
 * says as much, and this tile has to pass that on rather than launder an
 * estimate into a reading. So the label carries "est." and the meter is drawn
 * in the muted colour instead of the healthy green: someone trusting a
 * half-full battery on a device that is actually dead is the failure that
 * matters here.
 */
@Composable
private fun BatteryTile(device: DeviceStatus?, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    val pct = device?.batteryPct
    val measured = device?.measured == true
    StatTile(
        label = if (pct != null && !measured) "Battery (est.)" else "Battery",
        value = pct?.let { "${(it * 100).toInt()}%" } ?: "—",
        icon = RecallIcons.Battery,
        modifier = modifier,
        meter = {
            TileMeter(
                fraction = pct?.toFloat() ?: 0f,
                color = if (measured) colors.ok else colors.greyFaint,
            )
        },
    )
}

/**
 * BLE link quality. The connected/disconnected state is real; the bar count
 * is a coarse stand-in, since the relay does not sample RSSI after the GATT
 * connection is established.
 */
@Composable
private fun BleLinkTile(device: DeviceState, modifier: Modifier = Modifier) {
    val (label, bars) = when (device) {
        is DeviceState.Connected -> "Linked" to 3
        DeviceState.Scanning -> "Searching" to 1
        is DeviceState.Disconnected -> "Dropped" to 0
        DeviceState.Unknown -> "—" to 0
    }
    StatTile(
        label = "BLE link",
        value = label,
        icon = RecallIcons.Signal,
        modifier = modifier,
        meter = { TileSegments(filled = bars) },
    )
}

/**
 * The live-capture card. The elapsed clock and the live/idle state are real
 * (both come from [RelayConnectionState.Live]).
 *
 * **Partial placeholder.** The comp shows the sentence currently being
 * transcribed; the app has no live-transcript stream — transcripts are only
 * readable over HTTP after the fact — so the body falls back to the most
 * recent recording's preview, labelled as such.
 */
@Composable
private fun HearingNowCard(
    relay: RelayState,
    lastRecording: Segment?,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    val live = relay.connection as? RelayConnectionState.Live
    RecallCard(modifier = modifier, contentPadding = PaddingValues(18.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.SpaceBetween,
            verticalAlignment = Alignment.CenterVertically,
        ) {
            Row(
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(9.dp),
            ) {
                if (live != null) {
                    LiveBars()
                } else {
                    StatusDot(color = colors.greyFaint, size = 8.dp)
                }
                Text(
                    text = if (live != null) "Hearing now" else "Not capturing",
                    style = MaterialTheme.typography.titleSmall,
                    color = colors.ink,
                )
            }
            if (live != null) {
                Text(
                    text = formatHmMs(live.sinceMs),
                    style = MaterialTheme.typography.labelMedium,
                    color = colors.greyLight,
                )
            }
        }
        Row(
            modifier = Modifier.padding(top = 11.dp),
            verticalAlignment = Alignment.Top,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = when {
                    lastRecording != null && lastRecording.preview.isNotBlank() ->
                        lastRecording.preview
                    live != null -> "Listening. Nothing transcribed yet."
                    else -> "Your OpenRecall isn't streaming right now."
                },
                style = MaterialTheme.typography.bodyMedium,
                color = colors.inkMuted,
                modifier = Modifier.weight(1f),
            )
            if (live != null) PlaceholderTag(label = "Last heard")
        }
    }
}

/** Relay server health: reachability dot, 24h event count, version, and — */
@Composable
private fun RelayServerCard(server: Outcome<ServerStatus>, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    val status = (server as? Outcome.Success)?.value
    val online = status?.reachable == true
    RecallCard(
        modifier = modifier,
        contentPadding = PaddingValues(horizontal = 18.dp, vertical = 15.dp),
    ) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            StatusDot(color = if (online) colors.ok else colors.greyFaint, size = 7.dp)
            Column(Modifier.weight(1f)) {
                Text(
                    "Relay server",
                    style = MaterialTheme.typography.titleSmall,
                    color = colors.ink,
                )
                Text(
                    text = if (online) {
                        buildString {
                            append("${status.recentEvents24h} events today")
                            status.version?.let { append(" · v$it") }
                        }
                    } else {
                        "Unreachable"
                    },
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.grey,
                    modifier = Modifier.padding(top = 2.dp),
                )
            }
            if (online) {
                Text(
                    text = "${status.activeSessions} live",
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.greyLight,
                )
            }
        }
    }
}

/** A recent-recording card: title, relative time, preview and metadata. */
@Composable
private fun SegmentCard(
    segment: Segment,
    onClick: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    RecallCard(modifier = modifier, onClick = onClick) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            horizontalArrangement = Arrangement.spacedBy(10.dp),
            verticalAlignment = Alignment.Top,
        ) {
            Text(
                text = segment.displayTitle,
                style = MaterialTheme.typography.titleSmall,
                color = colors.ink,
                maxLines = 1,
                overflow = TextOverflow.Ellipsis,
                modifier = Modifier.weight(1f),
            )
            Text(
                text = formatRelative(System.currentTimeMillis(), segment.startedAt.toEpochMilli()),
                style = MaterialTheme.typography.labelMedium,
                color = colors.greyLight,
            )
        }
        Text(
            text = segment.preview.ifBlank { "No transcript yet." },
            style = MaterialTheme.typography.bodyMedium,
            color = colors.inkMuted,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 6.dp),
        )
        Text(
            text = "${formatHmMs(segment.durationMs)} · ${segment.transcriptCount} lines",
            style = MaterialTheme.typography.bodySmall,
            color = colors.greyLight,
            modifier = Modifier.padding(top = 8.dp),
        )
    }
}

/** The device hero's label, visual state and tone, resolved together so they
 *  can never disagree. */
private data class HeroStatus(
    val label: String,
    val tone: StatusTone,
    val visual: DeviceVisualState,
    val canRetry: Boolean,
)

private fun deviceStatus(relay: RelayState, provisioned: Boolean): HeroStatus = when {
    !provisioned -> HeroStatus(
        label = "Not connected",
        tone = StatusTone.Idle,
        visual = DeviceVisualState.Off,
        canRetry = false,
    )
    else -> when (val c = relay.connection) {
        is RelayConnectionState.Live -> HeroStatus(
            "Connected · listening", StatusTone.Healthy, DeviceVisualState.Connected, false,
        )
        is RelayConnectionState.BleConnected -> HeroStatus(
            "Linked · connecting", StatusTone.Working, DeviceVisualState.Searching, false,
        )
        is RelayConnectionState.SocketConnecting -> HeroStatus(
            "Connecting…", StatusTone.Working, DeviceVisualState.Searching, false,
        )
        RelayConnectionState.BleScanning -> HeroStatus(
            "Searching…", StatusTone.Working, DeviceVisualState.Searching, false,
        )
        is RelayConnectionState.Reconnecting -> HeroStatus(
            "Reconnecting…", StatusTone.Working, DeviceVisualState.Searching, false,
        )
        is RelayConnectionState.Failed -> HeroStatus(
            "Disconnected", StatusTone.Problem, DeviceVisualState.Off, true,
        )
        RelayConnectionState.Idle -> HeroStatus(
            "Not connected", StatusTone.Idle, DeviceVisualState.Off, true,
        )
    }.let { status ->
        // An Idle/active connection with a dropped BLE link still warrants
        // the manual retry affordance.
        if (relay.device is DeviceState.Disconnected) status.copy(canRetry = true) else status
    }
}

private fun deviceName(device: DeviceState): String =
    (device as? DeviceState.Connected)?.name ?: "OpenRecall"
