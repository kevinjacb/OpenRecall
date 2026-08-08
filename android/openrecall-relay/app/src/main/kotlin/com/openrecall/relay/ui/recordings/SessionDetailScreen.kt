package com.openrecall.relay.ui.recordings

import androidx.compose.foundation.background
import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.ExperimentalLayoutApi
import androidx.compose.foundation.layout.FlowRow
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.size
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.items
import androidx.compose.foundation.lazy.rememberLazyListState
import androidx.compose.foundation.shape.CircleShape
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Icon
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SnackbarHost
import androidx.compose.material3.SnackbarHostState
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.draw.clip
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import androidx.lifecycle.viewmodel.compose.viewModel
import androidx.lifecycle.viewmodel.initializer
import androidx.lifecycle.viewmodel.viewModelFactory
import com.openrecall.relay.core.ui.RecallTheme
import com.openrecall.relay.core.util.formatHmMs
import com.openrecall.relay.core.util.formatRelative
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.data.SpeakerCache
import com.openrecall.relay.data.SpeakerEntry
import com.openrecall.relay.data.personLabels
import com.openrecall.relay.domain.model.AudioSegment
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.SessionId
import com.openrecall.relay.domain.model.SessionSummary
import com.openrecall.relay.domain.model.TranscriptChunk
import com.openrecall.relay.ui.design.AccentChip
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.LoadingCard
import com.openrecall.relay.ui.design.PlaceholderTag
import com.openrecall.relay.ui.design.RecallCard
import com.openrecall.relay.ui.design.RecallDetailHeader
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.WaveformStrip

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
                    memoryRepo = RepositoryModule.repos.memoryRepository,
                )
            }
        },
    )
    // Auto-refresh while the screen is on top: a session that is still being
    // transcribed keeps filling in without a pull gesture.
    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { vm.startAutoRefresh() }
    LifecycleEventEffect(Lifecycle.Event.ON_PAUSE) { vm.stopAutoRefresh() }

    val state by vm.state.collectAsState()
    val isRefreshing by vm.isRefreshing.collectAsState()
    val youConfirmation by vm.youConfirmation.collectAsState()
    val speakerError by vm.speakerError.collectAsState()
    val memories by vm.memories.collectAsState()
    SessionDetailScreen(
        state = state,
        memories = memories,
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
 * Stateless session detail — the comp's recording screen: a back header, a
 * metadata line, the audio player, the memories this session produced, then
 * the transcript.
 *
 * Speaker labels are tappable, opening rename / reassign, which is how a
 * transcript gets from "Speaker 2" to a name.
 */
@Composable
fun SessionDetailScreen(
    state: SessionDetailUiState,
    memories: List<MemoryAtom> = emptyList(),
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
    val colors = RecallTheme.colors
    val snackbarHostState: SnackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(speakerError) {
        if (speakerError != null) {
            snackbarHostState.showSnackbar(message = speakerError)
            onDismissSpeakerError()
        }
    }
    Scaffold(
        modifier = modifier,
        containerColor = colors.canvas,
        snackbarHost = { SnackbarHost(hostState = snackbarHostState) },
    ) { padding ->
        Column(
            Modifier
                .padding(padding)
                .fillMaxSize()
                .background(colors.canvas)
                .statusBarsPadding(),
        ) {
            RecallDetailHeader(title = headerTitle(state), onBack = onBack)
            when (state) {
                is SessionDetailUiState.Loading ->
                    Column(Modifier.padding(20.dp)) { LoadingCard() }

                is SessionDetailUiState.Failed -> Box(
                    Modifier.fillMaxSize(),
                    contentAlignment = Alignment.Center,
                ) {
                    EmptyState(
                        title = "Couldn't load this recording",
                        body = state.reason,
                        ctaLabel = "Try again",
                        onCta = onRefresh,
                    )
                }

                is SessionDetailUiState.LoadedSummary -> Body(
                    summary = state.summary, events = null, memories = memories,
                    isRefreshing = isRefreshing, onRefresh = onRefresh,
                    speakerCache = speakerCache,
                    onRenameSpeaker = onRenameSpeaker,
                    onReassignSpeaker = onReassignSpeaker,
                    youConfirmation = youConfirmation,
                    onConfirmYou = onConfirmYou,
                    onDismissYouConfirmation = onDismissYouConfirmation,
                )

                is SessionDetailUiState.Loaded -> Body(
                    summary = state.summary, events = state.events, memories = memories,
                    isRefreshing = isRefreshing, onRefresh = onRefresh,
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
    memories: List<MemoryAtom>,
    isRefreshing: Boolean,
    onRefresh: () -> Unit,
    speakerCache: SpeakerCache,
    onRenameSpeaker: (speakerId: String, name: String) -> Unit,
    onReassignSpeaker: (fromId: String, toId: String) -> Unit,
    youConfirmation: YouConfirmationState,
    onConfirmYou: (name: String) -> Unit,
    onDismissYouConfirmation: () -> Unit,
) {
    val colors = RecallTheme.colors

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

    val listState = rememberLazyListState()
    // A transcript reads newest-last, and the newest hop is what you came for,
    // so open at the bottom. Only once per session — after that the position is
    // the reader's, and later events must not yank them back down.
    var landedAtLatest by remember { mutableStateOf(false) }
    // Fixed items ahead of the transcript in the LazyColumn: meta, player,
    // you-confirm, transcript-label, plus memories when present.
    val fixedItemCount = 4 + if (memories.isNotEmpty()) 1 else 0
    LaunchedEffect(events?.size) {
        val count = events?.size ?: 0
        if (!landedAtLatest && count > 0) {
            // The list clamps to its max scroll, so targeting the last index
            // lands on the true bottom rather than parking it at the top.
            listState.scrollToItem(fixedItemCount + count - 1)
            landedAtLatest = true
        }
    }

    PullToRefreshBox(
        isRefreshing = isRefreshing,
        onRefresh = onRefresh,
        modifier = Modifier.fillMaxSize(),
    ) {
        // Keyed by the stable event id — NOT by title, which would collide on
        // two transcripts with identical text and crash the LazyColumn.
        LazyColumn(
            state = listState,
            modifier = Modifier.fillMaxSize(),
            contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 32.dp),
        ) {
            item(key = "meta") {
                Text(
                    text = metaLine(summary),
                    style = MaterialTheme.typography.bodySmall,
                    color = colors.grey,
                    modifier = Modifier.padding(top = 4.dp),
                )
            }

            item(key = "player") {
                PlayerCard(summary, Modifier.padding(top = 16.dp))
            }

            // One-time "is this you?" prompt. Composes nothing unless the
            // state is Prompting, so it costs no space when idle.
            item(key = "you-confirm") {
                YouConfirmationBanner(
                    state = youConfirmation,
                    onConfirm = onConfirmYou,
                    onDismiss = onDismissYouConfirmation,
                )
            }

            if (memories.isNotEmpty()) {
                item(key = "memories") {
                    MemoriesSection(memories, Modifier.padding(top = 26.dp))
                }
            }

            item(key = "transcript-label") {
                Text(
                    text = "TRANSCRIPT",
                    style = MaterialTheme.typography.labelSmall,
                    color = colors.slate,
                    modifier = Modifier.padding(top = 28.dp, bottom = 14.dp),
                )
            }

            when {
                events == null -> item(key = "events-loading") { LoadingCard() }
                events.isEmpty() -> item(key = "events-empty") {
                    EmptyState(
                        title = "No transcript yet",
                        body = "This session hasn't produced any transcript events.",
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

/**
 * The audio player.
 *
 * **Placeholder.** The server exposes no audio download or streaming
 * endpoint for a session — only its transcript events — so there is nothing
 * to play. The card renders the comp's layout with a disabled transport and
 * a [WaveformStrip] shaped from the session id, marked so it doesn't read as
 * a broken control.
 */
@Composable
private fun PlayerCard(summary: SessionSummary, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    RecallCard(modifier = modifier, contentPadding = PaddingValues(16.dp)) {
        Row(
            modifier = Modifier.fillMaxWidth(),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(14.dp),
        ) {
            Box(
                Modifier
                    .size(48.dp)
                    .clip(CircleShape)
                    .background(colors.track),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    RecallIcons.Play,
                    contentDescription = "Playback unavailable",
                    tint = colors.greyLight,
                    modifier = Modifier.size(16.dp),
                )
            }
            Column(Modifier.weight(1f)) {
                WaveformStrip(seed = summary.id.value)
                Row(
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        "00:00",
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.greyFaint,
                    )
                    Text(
                        formatHmMs(summary.durationMs),
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.greyFaint,
                    )
                }
            }
        }
        Row(
            modifier = Modifier.fillMaxWidth().padding(top = 12.dp),
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            PlaceholderTag()
            Text(
                text = "Audio playback needs a session audio endpoint on the relay.",
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
            )
        }
    }
}

@OptIn(ExperimentalLayoutApi::class)
@Composable
private fun MemoriesSection(memories: List<MemoryAtom>, modifier: Modifier = Modifier) {
    val colors = RecallTheme.colors
    Column(modifier) {
        Row(
            verticalAlignment = Alignment.CenterVertically,
            horizontalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            Icon(
                RecallIcons.Sparkle,
                contentDescription = null,
                tint = colors.accent,
                modifier = Modifier.size(15.dp),
            )
            Text(
                "MEMORIES FROM THIS SESSION",
                style = MaterialTheme.typography.labelSmall,
                color = colors.slate,
            )
        }
        FlowRow(
            modifier = Modifier.padding(top = 10.dp),
            horizontalArrangement = Arrangement.spacedBy(8.dp),
            verticalArrangement = Arrangement.spacedBy(8.dp),
        ) {
            memories.forEach { atom -> AccentChip(label = atom.text) }
        }
    }
}

/**
 * One transcript hop: elapsed time in a fixed gutter, then the speaker and
 * their line. Tapping the speaker opens rename / reassign.
 */
@Composable
private fun EventRow(
    event: CaptureEvent,
    speakerCache: SpeakerCache,
    personLabels: Map<String, String> = emptyMap(),
    onRename: (speakerId: String, name: String) -> Unit,
    onReassign: (fromId: String, toId: String) -> Unit,
) {
    val colors = RecallTheme.colors
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

    var showMenu by remember { mutableStateOf(false) }
    var showRename by remember { mutableStateOf(false) }
    var showReassign by remember { mutableStateOf(false) }
    var renameText by remember { mutableStateOf("") }

    Row(
        modifier = Modifier.fillMaxWidth().padding(bottom = 20.dp),
        horizontalArrangement = Arrangement.spacedBy(14.dp),
    ) {
        Text(
            text = formatHmMs(event.startMs).removePrefix("00:"),
            style = MaterialTheme.typography.labelMedium,
            color = colors.greyFaint,
            modifier = Modifier.width(44.dp).padding(top = 2.dp),
        )
        Column(Modifier.weight(1f)) {
            if (chunk != null && speakerId != null) {
                Text(
                    text = displayName ?: "?",
                    style = MaterialTheme.typography.labelSmall,
                    color = colors.grey,
                    modifier = Modifier
                        .clickable { showMenu = true }
                        .padding(bottom = 3.dp),
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
                if (showRename) {
                    RenameSpeakerDialog(
                        value = renameText,
                        onValueChange = { renameText = it },
                        onConfirm = {
                            if (renameText.isNotBlank()) onRename(speakerId, renameText.trim())
                            showRename = false
                        },
                        onDismiss = { showRename = false },
                    )
                }
                if (showReassign) {
                    // The reassign targets are the other speakers the cache
                    // knows about — the server has no "merge into new" verb.
                    val others = speakerCache.snapshot().values.filter { it.speakerId != speakerId }
                    DropdownMenu(
                        expanded = true,
                        onDismissRequest = { showReassign = false },
                    ) {
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
                text = eventText(event),
                style = MaterialTheme.typography.bodyMedium,
                color = colors.inkSoft,
            )
        }
    }
}

@Composable
private fun RenameSpeakerDialog(
    value: String,
    onValueChange: (String) -> Unit,
    onConfirm: () -> Unit,
    onDismiss: () -> Unit,
) {
    val colors = RecallTheme.colors
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = colors.card,
        titleContentColor = colors.ink,
        title = { Text("Name this speaker") },
        text = {
            com.openrecall.relay.ui.design.RecallTextField(
                value = value,
                onValueChange = onValueChange,
                placeholder = "Display name",
            )
        },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Save") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

private fun headerTitle(state: SessionDetailUiState): String = when (state) {
    is SessionDetailUiState.Loaded -> sessionTitle(state.summary)
    is SessionDetailUiState.LoadedSummary -> sessionTitle(state.summary)
    else -> "Recording"
}

/** "34 min ago · 20:23 · 697 segments" — the comp's metadata line. */
private fun metaLine(summary: SessionSummary): String = buildString {
    append(formatRelative(System.currentTimeMillis(), summary.startedAt.toEpochMilli()))
    append(" · ")
    append(formatHmMs(summary.durationMs))
    append(" · ")
    append("${summary.transcriptCount} segments")
}

private fun eventText(event: CaptureEvent): String = when (event) {
    is TranscriptChunk -> event.text.ifBlank { "(empty transcript)" }
    is AudioSegment -> "Audio · ${event.byteCount} bytes"
}
