package com.openrecall.relay.ui.recordings

import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.derivedStateOf
import androidx.compose.runtime.getValue
import androidx.compose.runtime.remember
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.core.ui.toDisplayMessage
import com.openrecall.relay.core.util.formatHmMs
import com.openrecall.relay.core.util.formatRelative
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.ui.design.AccentChip
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.SecondaryButton
import com.openrecall.relay.ui.design.RecallCard
import com.openrecall.relay.ui.design.RecallGroupLabel
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.RecallScreenHeader
import com.openrecall.relay.ui.design.RecallSearchField

/**
 * Recordings route. Builds the [RecordingsViewModel] from the process
 * singleton and forwards row taps to [onOpen] (nav to recording detail).
 */
@Composable
fun RecordingsRoute(onOpen: (SegmentId) -> Unit, modifier: Modifier = Modifier) {
    val vm: RecordingsViewModel = viewModel(
        factory = viewModelFactory {
            initializer { RecordingsViewModel(RepositoryModule.repos.segment) }
        },
    )
    // Auto-refresh while the tab is on screen: new recordings appear, and a
    // session still being transcribed keeps growing, without a gesture.
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { vm.startAutoRefresh() }
    LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { vm.stopAutoRefresh() }

    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    val query by vm.query.collectAsState()
    RecordingsScreen(
        state = state,
        query = query,
        onQueryChange = vm::onQueryChange,
        isRefreshing = isRefreshing,
        onOpen = onOpen,
        onLoadMore = vm::onLoadMore,
        onRefresh = vm::onRefresh,
        modifier = modifier,
    )
}

/**
 * Stateless Recordings content — the comp's "Everything it heard": an
 * editorial header, a search field, then recordings grouped by recency
 * (Today / Yesterday / Earlier).
 *
 * Search results are *not* grouped: they come back ranked by the server as a
 * single unpaged result set, and bucketing them by day would imply a
 * chronology the ordering doesn't have.
 *
 * The list is a single [LazyColumn] carrying the header and search field as
 * items, so the whole screen scrolls as one surface. Reaching the last row
 * triggers [onLoadMore] (cursor pagination); pull-to-refresh resets to
 * page 1.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun RecordingsScreen(
    state: RecordingsUiState,
    query: String = "",
    onQueryChange: (String) -> Unit = {},
    isRefreshing: Boolean = false,
    onOpen: (SegmentId) -> Unit,
    onLoadMore: () -> Unit,
    onRefresh: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = modifier.fillMaxSize().background(colors.canvas),
    ) {
        when (state) {
            is RecordingsUiState.Loading -> Centered {
                CircularProgressIndicator(color = colors.accent)
            }

            is RecordingsUiState.Empty -> HeaderThen(query, onQueryChange) {
                EmptyState(
                    title = if (query.isBlank()) "No recordings yet" else "Nothing matches",
                    body = if (query.isBlank()) {
                        "Recordings from your OpenRecall will appear here."
                    } else {
                        "No transcript mentions “$query”."
                    },
                    modifier = Modifier.padding(top = 48.dp),
                )
            }

            is RecordingsUiState.Error -> HeaderThen(query, onQueryChange) {
                EmptyState(
                    title = "Couldn't load recordings",
                    body = state.reason,
                    ctaLabel = "Try again",
                    onCta = onRefresh,
                    modifier = Modifier.padding(top = 48.dp),
                )
            }

            is RecordingsUiState.Loaded -> LoadedList(
                state = state,
                query = query,
                onQueryChange = onQueryChange,
                onOpen = onOpen,
                onLoadMore = onLoadMore,
            )
        }
    }
}

@Composable
private fun LoadedList(
    state: RecordingsUiState.Loaded,
    query: String,
    onQueryChange: (String) -> Unit,
    onOpen: (SegmentId) -> Unit,
    onLoadMore: () -> Unit,
) {
    val listState = rememberLazyListState()
    val items = state.items
    val groups = remember(items, state.searching) {
        if (state.searching) {
            listOf(SegmentGroup("Matches", items))
        } else {
            groupByRecency(items)
        }
    }

    // Fire load-more when the last item scrolls into view and the list can
    // still grow. Suppressed while an inline loadError is showing — the
    // error row's Retry is the manual trigger then, so scrolling down to
    // read the error doesn't silently re-fire the request that just failed.
    val shouldLoadMore by remember(items.size, state.canLoadMore, state.loadError) {
        derivedStateOf {
            if (!state.canLoadMore || state.loadError != null) {
                false
            } else {
                val last = listState.layoutInfo.visibleItemsInfo.lastOrNull()?.index ?: 0
                last >= listState.layoutInfo.totalItemsCount - 2
            }
        }
    }
    LaunchedEffect(shouldLoadMore) { if (shouldLoadMore) onLoadMore() }

    LazyColumn(
        state = listState,
        modifier = Modifier.fillMaxSize().statusBarsPadding(),
        contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 28.dp),
    ) {
        item(key = "header") { RecordingsHeader(query, onQueryChange) }

        groups.forEach { group ->
            item(key = "label-${group.label}") {
                RecallGroupLabel(group.label, Modifier.padding(top = 26.dp, bottom = 12.dp))
            }
            items(
                count = group.items.size,
                key = { index -> group.items[index].id.value },
            ) { index ->
                val segment = group.items[index]
                SegmentCard(
                    segment = segment,
                    onClick = { onOpen(segment.id) },
                    modifier = Modifier.padding(bottom = 10.dp),
                )
            }
        }

        if (state.loadError != null) {
            item(key = "load-error") {
                InlineError(
                    message = state.loadError.toDisplayMessage(),
                    onRetry = onLoadMore,
                )
            }
        }
    }
}

@Composable
private fun RecordingsHeader(query: String, onQueryChange: (String) -> Unit) {
    RecallScreenHeader(eyebrow = "Recordings", hero = "Everything it heard")
    RecallSearchField(
        value = query,
        onValueChange = onQueryChange,
        placeholder = "Search transcripts",
        modifier = Modifier.padding(top = 16.dp),
    )
}

/** Header + search field with arbitrary content below — the empty and error
 *  branches keep the search box so the user can clear their query. */
@Composable
private fun HeaderThen(
    query: String,
    onQueryChange: (String) -> Unit,
    content: @Composable () -> Unit,
) {
    Column(
        Modifier
            .fillMaxSize()
            .statusBarsPadding()
            .padding(horizontal = 20.dp),
    ) {
        RecordingsHeader(query, onQueryChange)
        content()
    }
}

/**
 * One recording row: title, when it happened, the body line, and a metadata
 * strip.
 *
 * The body is the matched transcript window on a search result and the
 * preview otherwise — showing the preview for a search hit would leave the
 * user staring at a row with no visible reason to have matched.
 */
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
            text = (segment.matchSnippet ?: segment.preview).ifBlank { "No transcript yet." },
            style = MaterialTheme.typography.bodyMedium,
            color = colors.inkMuted,
            maxLines = 2,
            overflow = TextOverflow.Ellipsis,
            modifier = Modifier.padding(top = 6.dp),
        )
        Row(
            modifier = Modifier.fillMaxWidth().padding(top = 10.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Text(
                text = metaLine(segment),
                style = MaterialTheme.typography.bodySmall,
                color = colors.greyLight,
                modifier = Modifier.weight(1f),
            )
            if (segment.hasAudio) {
                Icon(
                    RecallIcons.Play,
                    contentDescription = "Has audio",
                    tint = colors.greyLight,
                    modifier = Modifier.size(12.dp),
                )
            }
            if (segment.memoryCount > 0) {
                AccentChip(label = memoryBadge(segment.memoryCount))
            }
        }
    }
}

/** "01:04 · 37 lines", plus a live marker while the recording is still open. */
private fun metaLine(segment: Segment): String = buildString {
    append(formatHmMs(segment.durationMs))
    append(" · ")
    append("${segment.transcriptCount} lines")
    if (!segment.closed) append(" · recording")
}

private fun memoryBadge(count: Int): String =
    if (count == 1) "1 memory" else "$count memories"

@Composable
private fun InlineError(message: String, onRetry: () -> Unit) {
    Column(Modifier.fillMaxWidth().padding(top = 16.dp)) {
        Text(
            text = message,
            style = MaterialTheme.typography.bodySmall,
            color = RecallTheme.colors.grey,
        )
        SecondaryButton(
            label = "Retry",
            onClick = onRetry,
            modifier = Modifier.padding(top = 10.dp),
        )
    }
}

@Composable
private fun Centered(content: @Composable () -> Unit) {
    Box(Modifier.fillMaxSize(), contentAlignment = Alignment.Center) { content() }
}

/** A recency bucket and its recordings, in the comp's Today/Yesterday/Earlier
 *  order. Empty buckets are dropped. */
internal data class SegmentGroup(val label: String, val items: List<Segment>)

/**
 * Bucket recordings by start time relative to now. The server returns them
 * newest-first and carries no grouping of its own, so the split is done here
 * rather than adding a field to [Segment].
 */
internal fun groupByRecency(
    segments: List<Segment>,
    nowMs: Long = System.currentTimeMillis(),
): List<SegmentGroup> {
    val dayMs = 24 * 60 * 60 * 1000L
    val today = mutableListOf<Segment>()
    val yesterday = mutableListOf<Segment>()
    val earlier = mutableListOf<Segment>()
    segments.forEach { segment ->
        val age = nowMs - segment.startedAt.toEpochMilli()
        when {
            age < dayMs -> today
            age < 2 * dayMs -> yesterday
            else -> earlier
        }.add(segment)
    }
    return listOf(
        SegmentGroup("Today", today),
        SegmentGroup("Yesterday", yesterday),
        SegmentGroup("Earlier", earlier),
    ).filter { it.items.isNotEmpty() }
}
