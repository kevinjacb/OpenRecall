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
import androidx.compose.material3.IconButton
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
import com.openrecall.relay.data.AudioSource
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.RepositoryModule
import com.openrecall.relay.data.SpeakerCache
import com.openrecall.relay.data.SpeakerEntry
import com.openrecall.relay.data.personLabels
import com.openrecall.relay.domain.model.AudioSegment
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.TranscriptChunk
import com.openrecall.relay.domain.model.Waveform
import com.openrecall.relay.ui.design.AccentChip
import com.openrecall.relay.ui.design.EmptyState
import com.openrecall.relay.ui.design.LoadingCard
import com.openrecall.relay.ui.design.RecallCard
import com.openrecall.relay.ui.design.RecallDetailHeader
import com.openrecall.relay.ui.design.RecallIcons
import com.openrecall.relay.ui.design.RecallTextField
import com.openrecall.relay.ui.design.WaveformScrubber
import com.openrecall.relay.ui.design.WaveformStrip

/** Bars in the scrubber. The server publishes fixed 500 ms buckets and the
 *  client downsamples, so this is a display constant and nothing else. */
private const val WAVEFORM_BARS = 34

/**
 * Recording-detail route. [onBack] pops the back stack — and is also what
 * runs after a successful delete, since the screen's subject no longer
 * exists.
 */
@Composable
fun SegmentDetailRoute(id: SegmentId, onBack: () -> Unit, modifier: Modifier = Modifier) {
    val vm: SegmentDetailViewModel = viewModel(
        factory = viewModelFactory {
            initializer {
                SegmentDetailViewModel(
                    id,
                    RepositoryModule.repos.segment,
                    speakerCache = RepositoryModule.repos.speakerCache,
                    speakerActions = RepositoryModule.repos.speakerActions,
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
    val actionError by vm.actionError.collectAsState()
    val memories by vm.memories.collectAsState()
    val waveform by vm.waveform.collectAsState()
    val audio by vm.audio.collectAsState()
    val deleted by vm.deleted.collectAsState()

    LaunchedEffect(deleted) { if (deleted) onBack() }

    SegmentDetailScreen(
        state = state,
        memories = memories,
        waveform = waveform,
        audio = audio,
        isRefreshing = isRefreshing,
        onRefresh = vm::onRefresh,
        onBack = onBack,
        onRename = vm::rename,
        onDelete = vm::delete,
        speakerCache = vm.speakerCache,
        onRenameSpeaker = vm::renameSpeaker,
        onReassignSpeaker = vm::reassignSpeaker,
        youConfirmation = youConfirmation,
        onConfirmYou = vm::confirmYou,
        onDismissYouConfirmation = vm::dismissYouConfirmation,
        actionError = actionError,
        onDismissActionError = vm::dismissActionError,
        modifier = modifier,
    )
}

/**
 * Stateless recording detail — a back header with the overflow menu, a
 * metadata line, the audio player, the memories this recording produced, then
 * the transcript.
 *
 * Speaker labels are tappable, opening rename / reassign, which is how a
 * transcript gets from "Person 2" to a name.
 */
@Composable
fun SegmentDetailScreen(
    state: SegmentDetailUiState,
    memories: List<MemoryAtom> = emptyList(),
    waveform: Waveform? = null,
    audio: AudioSource? = null,
    isRefreshing: Boolean = false,
    onRefresh: () -> Unit = {},
    onBack: () -> Unit,
    onRename: (String) -> Unit = {},
    onDelete: () -> Unit = {},
    speakerCache: SpeakerCache = SpeakerCache(),
    onRenameSpeaker: (speakerId: String, name: String) -> Unit = { _, _ -> },
    onReassignSpeaker: (fromId: String, toId: String) -> Unit = { _, _ -> },
    youConfirmation: YouConfirmationState = YouConfirmationState.Idle,
    onConfirmYou: (name: String) -> Unit = {},
    onDismissYouConfirmation: () -> Unit = {},
    actionError: String? = null,
    onDismissActionError: () -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    val snackbarHostState: SnackbarHostState = remember { SnackbarHostState() }
    LaunchedEffect(actionError) {
        if (actionError != null) {
            snackbarHostState.showSnackbar(message = actionError)
            onDismissActionError()
        }
    }
    val summary = (state as? SegmentDetailUiState.Loaded)?.summary
    var showRenameDialog by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }

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
            RecallDetailHeader(
                title = summary?.displayTitle ?: "Recording",
                onBack = onBack,
                trailing = if (summary != null) {
                    {
                        OverflowMenu(
                            onRename = { showRenameDialog = true },
                            onDelete = { confirmDelete = true },
                        )
                    }
                } else {
                    null
                },
            )
            when (state) {
                is SegmentDetailUiState.Loading, is SegmentDetailUiState.Deleted ->
                    Column(Modifier.padding(20.dp)) { LoadingCard() }

                is SegmentDetailUiState.Failed -> Box(
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

                is SegmentDetailUiState.Loaded -> Body(
                    summary = state.summary,
                    events = state.events,
                    memories = memories,
                    waveform = waveform,
                    audio = audio,
                    isRefreshing = isRefreshing,
                    onRefresh = onRefresh,
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

    if (showRenameDialog && summary != null) {
        RenameRecordingDialog(
            initial = summary.title.orEmpty(),
            onConfirm = { name ->
                showRenameDialog = false
                onRename(name)
            },
            onDismiss = { showRenameDialog = false },
        )
    }

    if (confirmDelete) {
        DeleteRecordingDialog(
            onConfirm = {
                confirmDelete = false
                onDelete()
            },
            onDismiss = { confirmDelete = false },
        )
    }
}

@Composable
private fun OverflowMenu(onRename: () -> Unit, onDelete: () -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val colors = RecallTheme.colors
    Box {
        IconButton(onClick = { expanded = true }) {
            Icon(
                RecallIcons.More,
                contentDescription = "More",
                tint = colors.inkMuted,
                modifier = Modifier.size(18.dp),
            )
        }
        DropdownMenu(expanded = expanded, onDismissRequest = { expanded = false }) {
            DropdownMenuItem(
                text = { Text("Rename") },
                onClick = {
                    expanded = false
                    onRename()
                },
            )
            DropdownMenuItem(
                text = { Text("Delete", color = colors.danger) },
                onClick = {
                    expanded = false
                    onDelete()
                },
            )
        }
    }
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
private fun Body(
    summary: Segment,
    events: List<CaptureEvent>,
    memories: List<MemoryAtom>,
    waveform: Waveform?,
    audio: AudioSource?,
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
    // from the first-appearance order of this recording's speakers. Computed
    // here (a @Composable context) rather than inside the LazyColumn content
    // lambda, which is a LazyListScope where `remember` is not callable.
    val orderedSpeakers = remember(events) {
        events.filterIsInstance<TranscriptChunk>()
            .mapNotNull { chunk ->
                val sid = chunk.speaker ?: return@mapNotNull null
                SpeakerEntry(
                    speakerId = sid,
                    name = chunk.speakerName ?: speakerCache.get(sid)?.name,
                    isWearer = chunk.isWearer,
                )
            }
            .distinctBy { it.speakerId }
    }
    val labels = remember(orderedSpeakers) { personLabels(orderedSpeakers) }

    val listState = rememberLazyListState()
    // A transcript reads newest-last, and the newest hop is what you came for,
    // so open at the bottom. Only once per recording — after that the position
    // is the reader's, and later events must not yank them back down.
    var landedAtLatest by remember { mutableStateOf(false) }
    // Fixed items ahead of the transcript inside the scroller: the transcript
    // label, plus memories when this recording produced any. Everything above
    // that (meta, player, you-confirm) is pinned outside the scroller.
    val fixedItemCount = 1 + if (memories.isNotEmpty()) 1 else 0
    LaunchedEffect(events.size) {
        if (!landedAtLatest && events.isNotEmpty()) {
            // The list clamps to its max scroll, so targeting the last index
            // lands on the true bottom rather than parking it at the top.
            listState.scrollToItem(fixedItemCount + events.size - 1)
            landedAtLatest = true
        }
    }

    Column(Modifier.fillMaxSize()) {
        // Pinned above the scroller. The transcript opens at its latest hop,
        // which used to carry the player off-screen with it — the player is the
        // one control you always want reachable, so it does not scroll.
        Column(Modifier.padding(horizontal = 20.dp)) {
            Text(
                text = metaLine(summary),
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 4.dp),
            )
            PlayerCard(
                summary = summary,
                waveform = waveform,
                audio = audio,
                modifier = Modifier.padding(top = 16.dp),
            )
            // One-time "is this you?" prompt. Composes nothing unless the
            // state is Prompting, so it costs no space when idle.
            YouConfirmationBanner(
                state = youConfirmation,
                onConfirm = onConfirmYou,
                onDismiss = onDismissYouConfirmation,
            )
        }

        PullToRefreshBox(
            isRefreshing = isRefreshing,
            onRefresh = onRefresh,
            modifier = Modifier.fillMaxWidth().weight(1f),
        ) {
            // Keyed by the stable event id — NOT by text, which would collide on
            // two transcripts with identical content and crash the LazyColumn.
            LazyColumn(
                state = listState,
                modifier = Modifier.fillMaxSize(),
                contentPadding = PaddingValues(start = 20.dp, end = 20.dp, bottom = 32.dp),
            ) {
                // Memories scroll with the transcript rather than sitting in the
                // pinned block: the chips wrap to as many lines as the text
                // needs, and an unbounded pinned block would squeeze the
                // transcript down to nothing on a chatty recording.
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

                if (events.isEmpty()) {
                    item(key = "events-empty") {
                        EmptyState(
                            title = "No transcript yet",
                            body = "This recording hasn't produced any transcript lines.",
                        )
                    }
                } else {
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
 * Three genuinely different states, and conflating them would mislead:
 *
 * * **Playable** — the server has a frame log for this recording, so there is
 *   a real waveform and a working transport.
 * * **No audio** — normal, not an error. Either `save_audio` was off when
 *   this was captured, or retention has since swept the audio while keeping
 *   the transcript. The copy says which, because "the recording expired but
 *   the transcript did not" is not what a user assumes by default.
 * * **Still recording** — the segment is open, so the server serves what
 *   exists so far and its length keeps moving.
 */
@Composable
private fun PlayerCard(
    summary: Segment,
    waveform: Waveform?,
    audio: AudioSource?,
    modifier: Modifier = Modifier,
) {
    val colors = RecallTheme.colors
    val player = rememberSegmentPlayer(if (summary.hasAudio) audio else null)
    val playback = player.state

    // Prefer the player's own duration once the stream is prepared: it is the
    // real decoded length, whereas the segment's duration is derived from
    // event timestamps and can differ by a frame or two.
    val durationMs = playback.durationMs.takeIf { it > 0 } ?: summary.durationMs
    val progress = if (durationMs > 0) {
        (playback.positionMs.toFloat() / durationMs).coerceIn(0f, 1f)
    } else {
        0f
    }
    val bars = remember(waveform) { waveform?.resampled(WAVEFORM_BARS).orEmpty() }

    // Where the finger is mid-drag, or null when nobody is scrubbing. While it
    // is set it wins over the player's own position for both the playhead and
    // the elapsed label, so the drag reads as continuous instead of fighting
    // the 200 ms position poll. The decoder is only asked to seek on release —
    // a drag across the strip would otherwise fire a seek per touch event.
    var scrubFraction by remember { mutableStateOf<Float?>(null) }
    // Nothing to scrub until the stream is prepared and has a real length.
    val seekable = playback.durationMs > 0
    val displayProgress = scrubFraction ?: progress
    val displayPositionMs = scrubFraction
        ?.let { (durationMs * it).toLong() }
        ?: playback.positionMs

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
                    .background(if (summary.hasAudio) colors.accentSoft else colors.track)
                    .then(
                        if (summary.hasAudio) {
                            Modifier.clickable { player.togglePlayPause() }
                        } else {
                            Modifier
                        },
                    ),
                contentAlignment = Alignment.Center,
            ) {
                Icon(
                    if (playback.playing) RecallIcons.Pause else RecallIcons.Play,
                    contentDescription = when {
                        !summary.hasAudio -> "No audio for this recording"
                        playback.playing -> "Pause"
                        else -> "Play"
                    },
                    tint = if (summary.hasAudio) colors.accentInk else colors.greyLight,
                    modifier = Modifier.size(16.dp),
                )
            }
            Column(Modifier.weight(1f)) {
                if (bars.isNotEmpty()) {
                    WaveformScrubber(
                        peaks = bars,
                        progress = displayProgress,
                        onScrub = if (seekable) ({ scrubFraction = it }) else null,
                        onScrubEnd = { fraction ->
                            player.seekToFraction(fraction)
                            scrubFraction = null
                        },
                    )
                } else {
                    WaveformStrip(seed = summary.id.value)
                }
                Row(
                    modifier = Modifier.fillMaxWidth().padding(top = 8.dp),
                    horizontalArrangement = Arrangement.SpaceBetween,
                ) {
                    Text(
                        formatHmMs(displayPositionMs),
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.greyFaint,
                    )
                    Text(
                        formatHmMs(durationMs),
                        style = MaterialTheme.typography.bodySmall,
                        color = colors.greyFaint,
                    )
                }
            }
        }

        val note = playerNote(summary, playback)
        if (note != null) {
            Text(
                text = note,
                style = MaterialTheme.typography.bodySmall,
                color = colors.grey,
                modifier = Modifier.padding(top = 12.dp),
            )
        }
    }
}

private fun playerNote(summary: Segment, playback: PlaybackState): String? = when {
    !summary.hasAudio ->
        "No audio kept for this recording. The transcript is retained even after " +
            "audio expires, so an older recording can be readable but not playable."
    playback.error != null -> playback.error
    playback.preparing -> "Loading audio…"
    !summary.closed -> "Still recording — playback covers what's arrived so far."
    else -> null
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
                "MEMORIES FROM THIS RECORDING",
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
    // the local optimistic rename applied). If neither has a name we
    // synthesize a stable "Person N" placeholder from first-appearance order,
    // so the user never sees a bare "null" or "?".
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
            RecallTextField(
                value = value,
                onValueChange = onValueChange,
                placeholder = "Display name",
            )
        },
        confirmButton = { TextButton(onClick = onConfirm) { Text("Save") } },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun RenameRecordingDialog(
    initial: String,
    onConfirm: (String) -> Unit,
    onDismiss: () -> Unit,
) {
    val colors = RecallTheme.colors
    var text by remember { mutableStateOf(initial) }
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = colors.card,
        titleContentColor = colors.ink,
        title = { Text("Rename recording") },
        text = {
            RecallTextField(
                value = text,
                onValueChange = {
                    // Clamp at the server's limit rather than letting the user
                    // type past it and take a 400 on save.
                    if (it.length <= SegmentDetailViewModel.TITLE_MAX_CHARS) text = it
                },
                placeholder = "Title",
            )
        },
        confirmButton = {
            TextButton(
                onClick = { onConfirm(text) },
                enabled = text.isNotBlank(),
            ) { Text("Save") }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel") } },
    )
}

@Composable
private fun DeleteRecordingDialog(onConfirm: () -> Unit, onDismiss: () -> Unit) {
    val colors = RecallTheme.colors
    AlertDialog(
        onDismissRequest = onDismiss,
        containerColor = colors.card,
        titleContentColor = colors.ink,
        textContentColor = colors.inkMuted,
        title = { Text("Delete this recording?") },
        text = {
            Text(
                "The transcript, its audio, and the memories extracted from it are " +
                    "removed from the relay. This can't be undone.",
            )
        },
        confirmButton = {
            TextButton(onClick = onConfirm) { Text("Delete", color = colors.danger) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text("Cancel", color = colors.slate) } },
    )
}

/** "34 min ago · 01:04 · 37 lines · 4 memories" — the comp's metadata line. */
private fun metaLine(summary: Segment): String = buildString {
    append(formatRelative(System.currentTimeMillis(), summary.startedAt.toEpochMilli()))
    append(" · ")
    append(formatHmMs(summary.durationMs))
    append(" · ")
    append("${summary.transcriptCount} lines")
    if (summary.memoryCount > 0) {
        append(" · ")
        append(if (summary.memoryCount == 1) "1 memory" else "${summary.memoryCount} memories")
    }
}

private fun eventText(event: CaptureEvent): String = when (event) {
    is TranscriptChunk -> event.text.ifBlank { "(empty transcript)" }
    is AudioSegment -> "Audio · ${event.byteCount} bytes"
}
