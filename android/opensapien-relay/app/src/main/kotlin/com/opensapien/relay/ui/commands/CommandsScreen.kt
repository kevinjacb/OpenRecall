package com.opensapien.relay.ui.commands

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.opensapien.relay.core.ui.SenseTheme
import com.opensapien.relay.data.Command
import com.opensapien.relay.data.CommandsViewModel
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.LoadingCard
import com.opensapien.relay.ui.design.SenseDetailHeader
import com.opensapien.relay.ui.design.SenseGroupLabel
import com.opensapien.relay.ui.design.SenseIcons
import java.time.Instant

/**
 * Stateless CommandsScreen. Takes the [CommandsViewModel.UiState] directly so
 * it is unit-testable without Hilt. The [CommandsRoute] wires the ViewModel +
 * lifecycle polling.
 *
 * The comp has no design for this screen — commands were never modelled in it
 * — so the layout borrows the vocabulary the comp does define: the detail
 * header used by session detail and the pairing flow, [SenseGroupLabel]
 * section labels, and the same card rows the Recordings list uses.
 *
 * Layout:
 *  - SenseDetailHeader("Commands")
 *  - Loading: shimmering [LoadingCard]s
 *  - Error: [EmptyState] with a Retry CTA
 *  - Ready: "In flight (n)" label + cards; "Done" label + the most recent
 *    failure (only if one exists)
 *  - Ready empty: [EmptyState]
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CommandsScreen(
    state: CommandsViewModel.UiState,
    onAck: (commandId: String) -> Unit,
    onRetry: () -> Unit,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onBack: (() -> Unit)? = null,
    modifier: Modifier = Modifier,
    now: Instant = Instant.now(),
) {
    val colors = SenseTheme.colors
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(colors.canvas)
            .statusBarsPadding(),
    ) {
        if (onBack != null) {
            SenseDetailHeader(title = "Commands", onBack = onBack)
        } else {
            Text(
                text = "Commands",
                style = MaterialTheme.typography.titleMedium,
                color = colors.ink,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 14.dp),
            )
        }
        // Pull-to-refresh wraps the content: a pull-down re-fetches the
        // active-command list via [CommandsViewModel.onRefresh].
        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxSize(),
        ) {
            when (state) {
                is CommandsViewModel.UiState.Loading -> LoadingPlaceholder()
                is CommandsViewModel.UiState.Error -> ErrorContent(
                    message = state.message,
                    onRetry = onRetry,
                )

                is CommandsViewModel.UiState.Ready -> ReadyContent(
                    commands = state.commands,
                    onAck = onAck,
                    now = now,
                )
            }
        }
    }
}

/**
 * Three shimmering cards rather than a centred spinner — the comp never uses
 * a spinner, and skeletons hold the layout the real rows will occupy.
 */
@Composable
private fun LoadingPlaceholder() {
    Column(
        modifier = Modifier
            .fillMaxSize()
            .padding(horizontal = 20.dp)
            .padding(top = 8.dp)
            .testTag("commands_loading"),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        repeat(3) { LoadingCard() }
    }
}

@Composable
private fun ErrorContent(message: String, onRetry: () -> Unit) {
    EmptyState(
        title = "Can't reach your relay",
        body = message,
        icon = SenseIcons.Link,
        ctaLabel = "Retry",
        onCta = onRetry,
        modifier = Modifier
            .fillMaxSize()
            .testTag("commands_retry"),
    )
}

@Composable
private fun ReadyContent(
    commands: List<Command>,
    onAck: (String) -> Unit,
    now: Instant,
) {
    val inFlight = commands.filter { it.status.isInFlight() }
    val mostRecentFailure = commands.firstOrNull { it.status.isTerminalFailure() }

    if (inFlight.isEmpty() && mostRecentFailure == null) {
        EmptyState(
            title = "No commands yet",
            body = "When the agent acts on your behalf — taking a photo, say — the " +
                "request shows up here while it runs.",
            icon = SenseIcons.Sparkle,
            modifier = Modifier.fillMaxSize(),
        )
        return
    }

    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = PaddingValues(horizontal = 20.dp, vertical = 8.dp),
        verticalArrangement = Arrangement.spacedBy(10.dp),
    ) {
        if (inFlight.isNotEmpty()) {
            item {
                SenseGroupLabel(
                    label = "In flight (${inFlight.size})",
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(bottom = 2.dp)
                        .testTag("commands_section_in_flight"),
                )
            }
            items(inFlight, key = { it.commandId }) { c ->
                CommandRow(command = c, now = now, onAck = onAck)
            }
        }
        if (mostRecentFailure != null) {
            item {
                SenseGroupLabel(
                    label = "Done",
                    modifier = Modifier
                        .fillMaxWidth()
                        .padding(top = 14.dp, bottom = 2.dp)
                        .testTag("commands_section_done"),
                )
            }
            item(key = "done_${mostRecentFailure.commandId}") {
                CommandRow(
                    command = mostRecentFailure,
                    now = now,
                    onAck = { /* no-op for terminal rows */ },
                )
            }
        }
    }
}
