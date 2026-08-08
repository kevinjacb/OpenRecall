package com.opensapien.relay.ui.recordings

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import androidx.compose.runtime.collectAsState
import com.opensapien.relay.core.ui.Spacing
import com.opensapien.relay.core.util.formatHmMs
import com.opensapien.relay.data.RepositoryModule
import com.opensapien.relay.data.SpeakerCache
import com.opensapien.relay.data.SpeakerEntry
import com.opensapien.relay.data.personLabels
import com.opensapien.relay.domain.model.AudioSegment
import com.opensapien.relay.domain.model.CaptureEvent
import com.opensapien.relay.domain.model.SessionId
import com.opensapien.relay.domain.model.SessionSummary
import com.opensapien.relay.domain.model.TranscriptChunk
import com.opensapien.relay.ui.design.EmptyState
import com.opensapien.relay.ui.design.LoadingCard
import com.opensapien.relay.ui.design.MetricCard
import com.opensapien.relay.ui.design.SenseTopBar
import com.opensapien.relay.ui.design.TopBarState
import androidx.compose.foundation.clickable
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.TextButton
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue

/**
 * SessionDetail route. Builds the [SessionDetailViewModel] for [id] from the
 * process singleton; [onBack] pops the back stack.
 */
@Composable
fun SessionDetailRoute(id: SessionId, onBack: () -> Unit, modifier: Modifier = Modifier) {
    val vm: SessionDetailViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                SessionDetailViewModel(
                    id,
                    RepositoryModule.repos.session,
                    speakerCache = RepositoryModule.repos.speakerCache,
                    speakerActions = RepositoryModule.repos.speakerActions,
                )
            }
        },
    )
    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    val youConfirmation by vm.youConfirmation.collectAsState()
    val speakerError by vm.speakerError.collectAsState()
    SessionDetailScreen(
        state = state,
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        onBack = onBack,
        speakerCache = vm.speakerCache,
        onRenameSpeaker = vm::renameSpeaker,
        onReassignSpeaker = vm::reassignSpeaker,
        youConfirmation = youConfirmation,
        onConfirmYou = vm::confirmYou,
        onDismissYouConfirmation = vm::dismissYouConfirmation,
        speakerError = speakerError,
        onDismissSpeakerError = vm::dismissSpeakerError,
        modifier = modifier,
    )
}

/**
 * Stateless SessionDetail content. A [SenseTopBar] with a back arrow over
 * the progressive body: summary [MetricCard], then the event timeline. New
 * events arriving grow the [LazyColumn] without a full re-render (the events
 * come in via a new [SessionDetailUiState.Loaded]).
 *
 * The body is wrapped in a [PullToRefreshBox] so a pull-down gesture re-
 * fetches the session summary + event timeline.
 */
@Composable
fun SessionDetailScreen(
    state: SessionDetailUiState,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onBack: () -> Unit,
    speakerCache: SpeakerCache = SpeakerCache(),
    onRenameSpeaker: (speakerId: String, name: String) -> Unit = { _, _ -> },
    onReassignSpeaker: (fromId: String, toId: String) -> Unit = { _, _ -> },
    youConfirmation: YouConfirmationState = YouConfirmationState.Idle,
    onConfirmYou: (name: String) -> Unit = {},
    onDismissYouConfirmation: () -> Unit = {},
    speakerError: String? = null,
    onDismissSpeakerError: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val snackbarHostState: SnackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(speakerError) {
        if (speakerError != null) {
            snackbarHostState.showSnackbar(message = speakerError)
            onDismissSpeakerError()
        }
    }
    Scaffold(
        modifier = modifier,
        topBar = { SenseTopBar(TopBarState(title = "Session", onBack = onBack)) },
        snackbarHost = { SnackbarHost(hostState = snackbarHostState) },
    ) { padding ->
        Box(modifier = Modifier.padding(padding).fillMaxSize()) {
            when (state) {
                is SessionDetailUiState.Loading -> Column(Modifier.padding(Spacing.md)) { LoadingCard() }
                is SessionDetailUiState.Failed -> Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    EmptyState(
                        title = "Couldn't load session",
                        body = state.reason,
                        ctaLabel = "Try again",
                        onCta = onRefresh,
                    )
                }
                is SessionDetailUiState.LoadedSummary -> Body(
                    state.summary, events = null, isRefreshing = isRefreshing, onRefresh = onRefresh,
                    speakerCache = speakerCache,
                    onRenameSpeaker = onRenameSpeaker,
                    onReassignSpeaker = onReassignSpeaker,
                    youConfirmation = youConfirmation,
                    onConfirmYou = onConfirmYou,
                    onDismissYouConfirmation = onDismissYouConfirmation,
                )
                is SessionDetailUiState.Loaded -> Body(
                    state.summary, events = state.events, isRefreshing = isRefreshing, onRefresh = onRefresh,
                    speakerCache = speakerCache,
                    onRenameSpeaker = onRenameSpeaker,
                    onReassignSpeaker = onReassignSpeaker,
                    youConfirmation = youConfirmation,
                    onConfirmYou = onConfirmYou,
                    onDismissYouConfirmation = onDismissYouConfirmation,
                )
            }
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Body(
    summary: SessionSummary,
    events: List<CaptureEvent>?,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    speakerCache: SpeakerCache = SpeakerCache(),
    onRenameSpeaker: (speakerId: String, name: String) -> Unit = { _, _ -> },
    onReassignSpeaker: (fromId: String, toId: String) -> Unit = { _, _ -> },
    youConfirmation: YouConfirmationState = YouConfirmationState.Idle,
    onConfirmYou: (name: String) -> Unit = {},
    onDismissYouConfirmation: () -> Unit = {},
) {
    // "Person N" placeholder labels for unnamed non-wearer speakers, built
    // from the first-appearance order of this session's transcript speakers.
    // Computed here (a @Composable context) rather than inside the LazyColumn
    // content lambda (a LazyListScope, not @Composable — `remember` is not
    // callable there). Memoized per `events` change; cheap to recompute.
    val orderedSpeakers = remember(events) {
        events?.filterIsInstance<TranscriptChunk>()
            ?.mapNotNull { chunk ->
                val sid = chunk.speaker ?: return@mapNotNull null
                SpeakerEntry(
                    speakerId = sid,
                    name = chunk.speakerName ?: speakerCache.get(sid)?.name,
                    isWearer = chunk.isWearer,
                )
            }
            ?.distinctBy { it.speakerId }
            ?: emptyList()
    }
    val labels = remember(orderedSpeakers) { personLabels(orderedSpeakers) }

    // The timeline is keyed by the stable event id (NOT the design system's
    // `timeline()` helper, which keys by title and would crash a LazyColumn
    // on two transcripts with identical text).
    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = Modifier.fillMaxSize(),
    ) {
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
            // One-time "is this you?" prompt, above the timeline. Composes
            // nothing unless the state is Prompting, so it costs no space
            // when idle/done.
            item(key = "you-confirm") {
                YouConfirmationBanner(
                    state = youConfirmation,
                    onConfirm = onConfirmYou,
                    onDismiss = onDismissYouConfirmation,
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
                else -> {
                    items(events, key = { it.id }) { event ->
                        EventRow(
                            event = event,
                            speakerCache = speakerCache,
                            personLabels = labels,
                            onRename = onRenameSpeaker,
                            onReassign = onReassignSpeaker,
                        )
                    }
                }
            }
        }
    }
}

@Composable
private fun EventRow(
    event: CaptureEvent,
    speakerCache: SpeakerCache = SpeakerCache(),
    personLabels: Map<String, String> = emptyMap(),
    onRename: (speakerId: String, name: String) -> Unit = { _, _ -> },
    onReassign: (fromId: String, toId: String) -> Unit = { _, _ -> },
) {
    // Speaker label resolution. TranscriptChunks carry the server-resolved
    // speakerName (real null when the hop had no speaker / speaker unknown).
    // We fall back to the cache (a prior hop may have named this speaker, or
    // the local optimistic rename applied). If neither has a name we synthesize
    // a stable "Person N" placeholder (client-only) from first-appearance
    // order, so the user never sees a bare "null" or "?".
    val chunk = event as? TranscriptChunk
    val speakerId = chunk?.speaker
    val realName: String? = when {
        chunk == null || speakerId == null -> null
        chunk.speakerName != null -> chunk.speakerName
        speakerCache.get(speakerId)?.name != null -> speakerCache.get(speakerId)!!.name
        else -> null
    }
    val displayName: String? = realName ?: personLabels[speakerId]

    // Rename dialog state.
    var showRename by remember { mutableStateOf(false) }
    var renameText by remember { mutableStateOf("") }
    // Per-row dropdown menu (Rename / Reassign…).
    var showMenu by remember { mutableStateOf(false) }
    // Reassign picker menu.
    var showReassign by remember { mutableStateOf(false) }

    Column(modifier = Modifier.fillMaxWidth().padding(vertical = Spacing.xs)) {
        // Speaker tag — tappable when this hop has a speaker (opens Rename /
        // Reassign menu). Non-transcript rows render no tag.
        if (chunk != null && speakerId != null) {
            Text(
                text = displayName ?: "?",
                style = MaterialTheme.typography.labelMedium,
                color = MaterialTheme.colorScheme.primary,
                modifier = Modifier
                    .padding(bottom = Spacing.xs)
                    .clickable { showMenu = true },
            )
            DropdownMenu(expanded = showMenu, onDismissRequest = { showMenu = false }) {
                DropdownMenuItem(
                    text = { Text("Rename") },
                    onClick = {
                        showMenu = false
                        renameText = realName ?: ""
                        showRename = true
                    },
                )
                DropdownMenuItem(
                    text = { Text("Reassign to…") },
                    onClick = {
                        showMenu = false
                        showReassign = true
                    },
                )
            }
            // Rename dialog.
            if (showRename) {
                AlertDialog(
                    onDismissRequest = { showRename = false },
                    title = { Text("Name speaker") },
                    text = {
                        OutlinedTextField(
                            value = renameText,
                            onValueChange = { renameText = it },
                            singleLine = true,
                            label = { Text("Display name") },
                        )
                    },
                    confirmButton = {
                        TextButton(onClick = {
                            if (renameText.isNotBlank()) onRename(speakerId, renameText.trim())
                            showRename = false
                        }) { Text("Save") }
                    },
                    dismissButton = {
                        TextButton(onClick = { showRename = false }) { Text("Cancel") }
                    },
                )
            }
            // Reassign picker: list the other known speakers from the cache.
            if (showReassign) {
                val others = speakerCache.snapshot().values.filter { it.speakerId != speakerId }
                DropdownMenu(expanded = showReassign, onDismissRequest = { showReassign = false }) {
                    if (others.isEmpty()) {
                        DropdownMenuItem(
                            text = { Text("No other speakers yet") },
                            onClick = { showReassign = false },
                        )
                    } else {
                        others.forEach { entry ->
                            DropdownMenuItem(
                                text = { Text(entry.name ?: personLabels[entry.speakerId] ?: "?") },
                                onClick = {
                                    showReassign = false
                                    onReassign(speakerId, entry.speakerId)
                                },
                            )
                        }
                    }
                }
            }
        }
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
