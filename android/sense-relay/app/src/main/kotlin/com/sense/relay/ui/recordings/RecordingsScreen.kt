package com.sense.relay.ui.recordings

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.Card
import androidx.compose.material3.CardDefaults
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.sense.relay.core.ui.Spacing
import com.sense.relay.core.ui.toDisplayMessage
import com.sense.relay.core.util.formatRelative
import com.sense.relay.data.RepositoryModule
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.SectionHeader

/**
 * Recordings route. Builds the [RecordingsViewModel] from the process
 * singleton and forwards row taps to [onOpen] (nav to SessionDetail).
 */
@Composable
fun RecordingsRoute(onOpen: (SessionId) -> Unit, modifier: Modifier = Modifier) {
    val vm: RecordingsViewModel = viewModel(
        factory = viewModelFactory {
            initializer { RecordingsViewModel(RepositoryModule.repos.session) }
        },
    )
    val state by vm.state.collectAsState()
    RecordingsScreen(
        state = state,
        onOpen = onOpen,
        onLoadMore = vm::onLoadMore,
        modifier = modifier,
    )
}

/**
 * Stateless Recordings content. A single [LazyColumn] of session rows;
 * when the last row is reached, [onLoadMore] fires (cursor pagination).
 * Loading is a centered spinner; Empty/Error are calm [EmptyState]s.
 */
@Composable
fun RecordingsScreen(
    state: RecordingsUiState,
    onOpen: (SessionId) -> Unit,
    onLoadMore: () -> Unit,
    modifier: Modifier = Modifier,
) {
    when (state) {
        is RecordingsUiState.Loading -> Centered(modifier) { CircularProgressIndicator() }
        is RecordingsUiState.Empty -> Centered(modifier) {
            EmptyState(
                title = "No recordings yet",
                body = "Sessions from your Sense device will appear here.",
            )
        }
        is RecordingsUiState.Error -> Centered(modifier) {
            EmptyState(
                title = "Couldn't load recordings",
                body = state.reason,
                ctaLabel = "Try again",
                onCta = onLoadMore,
            )
        }
        is RecordingsUiState.Loaded -> LoadedList(state, onOpen, onLoadMore, modifier)
    }
}

@Composable
private fun LoadedList(
    state: RecordingsUiState.Loaded,
    onOpen: (SessionId) -> Unit,
    onLoadMore: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val listState = rememberLazyListState()
    val items = state.items
    // Fire load-more when the last item scrolls into view and the list can grow.
    val shouldLoadMore by remember(items.size, state.canLoadMore) {
        derivedStateOf {
            if (!state.canLoadMore) false
            else {
                val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
                last >= items.size - 1
            }
        }
    }
    LaunchedEffect(shouldLoadMore) {
        if (shouldLoadMore) onLoadMore()
    }

    Column(modifier = modifier.fillMaxSize()) {
        SectionHeader(
            title = "Recordings",
            modifier = Modifier.padding(horizontal = Spacing.md, vertical = Spacing.sm),
        )
        LazyColumn(
            state = listState,
            // weight(1f) so the list takes the remaining height after the
            // SectionHeader; without it, fillMaxSize measures the list at the
            // full parent height and the header's height clips the last rows.
            modifier = Modifier.weight(1f).fillMaxWidth(),
            contentPadding = androidx.compose.foundation.layout.PaddingValues(Spacing.md),
            verticalArrangement = Arrangement.spacedBy(Spacing.sm),
        ) {
            items(items, key = { it.id.value }) { session ->
                SessionRow(session, onClick = { onOpen(session.id) })
            }
            // Inline paging-error row: items are still shown, so the failure
            // is a small "Retry" affordance at the list's end — not a
            // full-screen error (which would discard the loaded sessions).
            if (state.loadError != null) {
                item(key = "load-error") {
                    InlineErrorRow(
                        message = state.loadError.toDisplayMessage(),
                        onRetry = onLoadMore,
                    )
                }
            }
        }
    }
}

@Composable
private fun InlineErrorRow(message: String, onRetry: () -> Unit) {
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .padding(vertical = Spacing.xs),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodySmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
            modifier = Modifier.weight(1f),
        )
        TextButton(onClick = onRetry) { Text("Retry") }
    }
}

@Composable
private fun SessionRow(session: SessionSummary, onClick: () -> Unit) {
    Card(
        modifier = Modifier
            .fillMaxWidth()
            .clickable(onClick = onClick),
        colors = CardDefaults.cardColors(
            containerColor = MaterialTheme.colorScheme.surface,
            contentColor = MaterialTheme.colorScheme.onSurface,
        ),
    ) {
        Column(modifier = Modifier.padding(Spacing.md)) {
            Row(
                modifier = Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text(
                    text = shortId(session.id),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
                Text(
                    text = formatRelative(System.currentTimeMillis(), session.startedAt.toEpochMilli()),
                    style = MaterialTheme.typography.labelMedium,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
            Text(
                text = session.preview.ifBlank { "(no transcript yet)" },
                style = MaterialTheme.typography.bodyMedium,
                color = MaterialTheme.colorScheme.onSurface,
                maxLines = 2,
                overflow = TextOverflow.Ellipsis,
            )
            Text(
                text = "${session.transcriptCount} transcript" +
                    if (session.transcriptCount == 1) "" else "s",
                style = MaterialTheme.typography.bodySmall,
                color = MaterialTheme.colorScheme.onSurfaceVariant,
            )
        }
    }
}

@Composable
private fun Centered(modifier: Modifier = Modifier, content: @Composable () -> Unit) {
    Box(
        modifier = modifier.fillMaxSize().padding(Spacing.lg),
        contentAlignment = Alignment.Center,
    ) { content() }
}

/** First 8 chars of the session id — enough to disambiguate at a glance. */
private fun shortId(id: SessionId): String = id.value.take(8)
