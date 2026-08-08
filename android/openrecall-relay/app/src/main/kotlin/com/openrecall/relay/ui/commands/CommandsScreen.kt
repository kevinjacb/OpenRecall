package com.openrecall.relay.ui.commands

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.Row
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
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.data.Command
import com.openrecall.relay.data.CommandsViewModel
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.LoadingCard
import com.openrecall.relay.ui.design.SecondaryButton
import com.openrecall.relay.ui.design.RecallDetailHeader
import com.openrecall.relay.ui.design.RecallGroupLabel
import com.openrecall.relay.ui.design.RecallIcons
import java.time.Instant

/**
 * Stateless CommandsScreen. Takes the [CommandsViewModel.UiState] directly so
 * it is unit-testable without Hilt. The [CommandsRoute] wires the ViewModel +
 * lifecycle polling.
 *
 * The comp has no design for this screen — commands were never modelled in it
 * — so the layout borrows the vocabulary the comp does define: the detail
 * header used by session detail and the pairing flow, [RecallGroupLabel]
 * section labels, and the same card rows the Recordings list uses.
 *
 * Layout:
 *  - RecallDetailHeader("Commands")
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
    onIssue: (type: String) -> Unit = {},
    issueError: String? = null,
    modifier: Modifier = Modifier,
    now: Instant = Instant.now(),
) {
    val colors = RecallTheme.colors
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(colors.canvas)
            .statusBarsPadding(),
    ) {
        if (onBack != null) {
            RecallDetailHeader(title = "Commands", onBack = onBack)
        } else {
            Text(
                text = "Commands",
                style = MaterialTheme.typography.titleMedium,
                color = colors.ink,
                modifier = Modifier.padding(horizontal = 20.dp, vertical = 14.dp),
            )
        }
        IssueRow(onIssue = onIssue, error = issueError)
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
 * Manual one-shot controls for the wearable's microphone gate.
 *
 * These are the raw verbs, and they are deliberately here rather than on
 * Settings: the Settings switch is *durable desired state* the relay
 * reconciles the device against, while these are a single command sent now.
 * A command reaches the device only while it is connected and does not
 * survive a reconnect, so pausing capture from here is a temporary act — if
 * you want it to stick, use the Settings toggle.
 */
@Composable
private fun IssueRow(onIssue: (String) -> Unit, error: String?) {
    val colors = RecallTheme.colors
    Column(Modifier.padding(horizontal = 20.dp)) {
        RecallGroupLabel("Send now", Modifier.padding(bottom = 10.dp))
        Row(horizontalArrangement = Arrangement.spacedBy(10.dp)) {
            SecondaryButton(
                label = "Pause mic",
                onClick = { onIssue("stop_audio") },
                modifier = Modifier.weight(1f),
            )
            SecondaryButton(
                label = "Resume mic",
                onClick = { onIssue("start_audio") },
                modifier = Modifier.weight(1f),
            )
        }
        Text(
            text = error ?: "One-shot, and only while the device is connected. " +
                "The Settings toggle is the durable one.",
            style = MaterialTheme.typography.bodySmall,
            color = if (error != null) colors.danger else colors.grey,
            modifier = Modifier.padding(top = 8.dp, bottom = 4.dp),
        )
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
        icon = RecallIcons.Link,
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
            icon = RecallIcons.Sparkle,
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
                RecallGroupLabel(
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
                RecallGroupLabel(
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
