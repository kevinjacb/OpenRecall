package com.sense.relay.ui.commands

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.Button
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.unit.dp
import com.sense.relay.core.ui.Spacing
import com.sense.relay.data.Command
import com.sense.relay.data.CommandsViewModel
import com.sense.relay.ui.design.SectionHeader
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState
import java.time.Instant

/**
 * Stateless CommandsScreen. Takes the [CommandsViewModel.UiState]
 * directly so it is unit-testable without Hilt. The
 * [CommandsRoute] wires the ViewModel + lifecycle polling.
 *
 * Layout:
 *  - SenseTopBar("Commands")
 *  - Loading: full-screen CircularProgressIndicator
 *  - Error: error message + Retry button
 *  - Ready: "In flight (n)" section + rows; "Done" section +
 *    most recent failure row (only if a failure exists)
 *  - Ready empty: EmptyState body only
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CommandsScreen(
    state: CommandsViewModel.UiState,
    onAck: (commandId: String) -> Unit,
    onRetry: () -> Unit,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
    now: Instant = Instant.now(),
) {
    Column(modifier = modifier.fillMaxSize()) {
        SenseTopBar(state = TopBarState(title = "Commands"))
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

@Composable
private fun LoadingPlaceholder() {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .testTag("commands_loading"),
        contentAlignment = Alignment.Center,
    ) {
        CircularProgressIndicator()
    }
}

@Composable
private fun ErrorContent(message: String, onRetry: () -> Unit) {
    Box(
        modifier = Modifier
            .fillMaxSize()
            .padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) {
        Column(
            verticalArrangement = Arrangement.spacedBy(Spacing.md),
            horizontalAlignment = Alignment.CenterHorizontally,
        ) {
            Text(
                text = message,
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
            )
            Button(
                onClick = onRetry,
                modifier = Modifier.testTag("commands_retry"),
            ) {
                Text("Retry")
            }
        }
    }
}

@Composable
private fun ReadyContent(
    commands: List<Command>,
    onAck: (String) -> Unit,
    now: Instant,
) {
    val inFlight = commands.filter { it.status.isInFlight() }
    val mostRecentFailure = commands.firstOrNull { it.status.isTerminalFailure() }

    when {
        inFlight.isEmpty() && mostRecentFailure == null -> {
            Box(
                modifier = Modifier
                    .fillMaxSize()
                    .padding(Spacing.lg),
                contentAlignment = Alignment.Center,
            ) {
                Text(
                    text = "No commands yet — try saying \"take a photo\" to the agent.",
                    style = MaterialTheme.typography.bodyMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
        else -> {
            LazyColumn(
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(Spacing.md),
                verticalArrangement = Arrangement.spacedBy(Spacing.sm),
            ) {
                if (inFlight.isNotEmpty()) {
                    item {
                        SectionHeader(
                            title = "In flight (${inFlight.size})",
                            modifier = Modifier.testTag("commands_section_in_flight"),
                        )
                    }
                    items(inFlight, key = { it.commandId }) { c ->
                        CommandRow(
                            command = c,
                            now = now,
                            onAck = onAck,
                        )
                    }
                }
                if (mostRecentFailure != null) {
                    item {
                        SectionHeader(
                            title = "Done",
                            modifier = Modifier.testTag("commands_section_done"),
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
    }
}
