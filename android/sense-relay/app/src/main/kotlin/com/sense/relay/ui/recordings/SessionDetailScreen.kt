package com.sense.relay.ui.recordings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.sense.relay.core.ui.Spacing
import com.sense.relay.core.util.formatHmMs
import com.sense.relay.data.RepositoryModule
import com.sense.relay.domain.model.AudioSegment
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.domain.model.TranscriptChunk
import com.sense.relay.ui.design.EmptyState
import com.sense.relay.ui.design.LoadingCard
import com.sense.relay.ui.design.MetricCard
import com.sense.relay.ui.design.SenseTopBar
import com.sense.relay.ui.design.TopBarState

/**
 * SessionDetail route. Builds the [SessionDetailViewModel] for [id] from the
 * process singleton; [onBack] pops the back stack.
 */
@Composable
fun SessionDetailRoute(id: SessionId, onBack: () -> Unit, modifier: Modifier = Modifier) {
    val vm: SessionDetailViewModel = viewModel(
        factory = viewModelFactory {
            initializer { SessionDetailViewModel(id, RepositoryModule.repos.session) }
        },
    )
    val state by vm.state.collectAsState()
    SessionDetailScreen(state = state, onBack = onBack, modifier = modifier)
}

/**
 * Stateless SessionDetail content. A [SenseTopBar] with a back arrow over
 * the progressive body: summary [MetricCard], then the event timeline. New
 * events arriving grow the [LazyColumn] without a full re-render (the events
 * come in via a new [SessionDetailUiState.Loaded]).
 */
@Composable
fun SessionDetailScreen(
    state: SessionDetailUiState,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    Scaffold(
        modifier = modifier,
        topBar = { SenseTopBar(TopBarState(title = "Session", onBack = onBack)) },
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            when (state) {
                is SessionDetailUiState.Loading -> Column(Modifier.padding(Spacing.md)) { LoadingCard() }
                is SessionDetailUiState.Failed -> Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    EmptyState(title = "Couldn't load session", body = state.reason)
                }
                is SessionDetailUiState.LoadedSummary -> Body(state.summary, events = null)
                is SessionDetailUiState.Loaded -> Body(state.summary, events = state.events)
            }
        }
    }
}

@Composable
private fun Body(summary: SessionSummary, events: List<CaptureEvent>?) {
    // The timeline is keyed by the stable event id (NOT the design system's
    // `timeline()` helper, which keys by title and would crash a LazyColumn
    // on two transcripts with identical text).
    LazyColumn(
        modifier = Modifier.fillMaxSize(),
        contentPadding = androidx.compose.foundation.layout.PaddingValues(Spacing.md),
        verticalArrangement = Arrangement.spacedBy(Spacing.sm),
    ) {
        item(key = "summary") {
            MetricCard(
                title = "Duration",
                value = formatHmMs(summary.durationMs),
                subtitle = "${summary.transcriptCount} transcripts",
            )
        }
        when {
            events == null -> item(key = "events-loading") { LoadingCard() }
            events.isEmpty() -> item(key = "events-empty") {
                EmptyState(
                    title = "No events",
                    body = "This session has no transcript events yet.",
                )
            }
            else -> items(events, key = { it.id }) { event -> EventRow(event) }
        }
    }
}

@Composable
private fun EventRow(event: CaptureEvent) {
    Column(modifier = Modifier.fillMaxWidth().padding(vertical = Spacing.xs)) {
        Text(
            text = eventTitle(event),
            style = MaterialTheme.typography.bodyMedium,
            color = MaterialTheme.colorScheme.onSurface,
        )
        Text(
            text = "+${formatHmMs(event.startMs)}",
            style = MaterialTheme.typography.labelSmall,
            color = MaterialTheme.colorScheme.onSurfaceVariant,
        )
    }
}

private fun eventTitle(event: CaptureEvent): String = when (event) {
    is TranscriptChunk -> event.text.ifBlank { "(empty transcript)" }
    is AudioSegment -> "Audio · ${event.byteCount} bytes"
}
