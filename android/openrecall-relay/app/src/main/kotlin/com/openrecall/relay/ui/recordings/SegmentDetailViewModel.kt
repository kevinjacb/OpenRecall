package com.openrecall.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.openrecall.relay.core.RecallLog
import com.openrecall.relay.core.result.Outcome
import com.openrecall.relay.core.ui.toDisplayMessage
import com.openrecall.relay.core.util.AUTO_REFRESH_INTERVAL_MS
import com.openrecall.relay.core.util.launchAutoRefresh
import com.openrecall.relay.data.AudioSource
import com.openrecall.relay.data.MemoryAtom
import com.openrecall.relay.data.NoopSpeakerActions
import com.openrecall.relay.data.SegmentRepository
import com.openrecall.relay.data.SpeakerActions
import com.openrecall.relay.data.SpeakerCache
import com.openrecall.relay.domain.model.CaptureEvent
import com.openrecall.relay.domain.model.Segment
import com.openrecall.relay.domain.model.SegmentId
import com.openrecall.relay.domain.model.TranscriptChunk
import com.openrecall.relay.domain.model.Waveform
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.Job
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.map
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * What the recording-detail screen renders. Sealed for exhaustive `when`
 * rendering.
 */
sealed interface SegmentDetailUiState {
    data object Loading : SegmentDetailUiState
    data class Loaded(
        val summary: Segment,
        val events: List<CaptureEvent>,
    ) : SegmentDetailUiState
    data class Failed(val reason: String) : SegmentDetailUiState
    /** The recording was deleted from this screen; the host should pop back. */
    data object Deleted : SegmentDetailUiState
}

/**
 * One-time "is this you? what should I call you?" prompt for the wearer.
 *
 * - [Idle]: nothing to show (no wearer seen yet, or already named).
 * - [Prompting]: the first transcript hop flagged `isWearer` whose resolved
 *   name is still null or the placeholder `"You"`. Carries the wearer's
 *   stable speaker id so [SegmentDetailViewModel.confirmYou] knows who to
 *   rename.
 * - [Done]: the user confirmed a name; the prompt won't re-show this session.
 *
 * Not persisted: if the app restarts while the wearer is still called "You",
 * the prompt re-shows — the right behaviour for a confirmation the user might
 * have dismissed accidentally.
 */
sealed interface YouConfirmationState {
    data object Idle : YouConfirmationState
    data class Prompting(val wearerId: String) : YouConfirmationState
    data object Done : YouConfirmationState
}

/**
 * Recording-detail ViewModel.
 *
 * `GET /segments/{id}` returns the summary and the transcript in one payload,
 * so unlike the old session screen there is no split "summary first, events
 * later" state to reduce — a single fetch either lands or it doesn't.
 *
 * The audio source, waveform and memories are fetched separately and are all
 * *enrichments*: each failure is silent and simply leaves its section absent.
 * A recording with no audio is a normal outcome (the frame log may have been
 * swept by retention, or `save_audio` was off when it was captured), not an
 * error worth putting a banner over the transcript.
 *
 * Refresh: [observeSegment] is a one-shot cold flow, so [onRefresh] bumps
 * [revision] and [flatMapLatest] re-collects it.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SegmentDetailViewModel(
    private val id: SegmentId,
    private val repo: SegmentRepository,
    val speakerCache: SpeakerCache = SpeakerCache(),
    private val speakerActions: SpeakerActions = NoopSpeakerActions,
) : ViewModel() {

    private val _memories = MutableStateFlow<List<MemoryAtom>>(emptyList())
    /** Atoms extracted from this recording, for the "memories from this
     *  recording" chip row. */
    val memories: StateFlow<List<MemoryAtom>> = _memories.asStateFlow()

    private val _waveform = MutableStateFlow<Waveform?>(null)
    /** Peaks behind the scrubber; null when the recording has no audio. */
    val waveform: StateFlow<Waveform?> = _waveform.asStateFlow()

    private val _audio = MutableStateFlow<AudioSource?>(null)
    /** The stream + auth header for the player; null until resolved. */
    val audio: StateFlow<AudioSource?> = _audio.asStateFlow()

    private val _deleted = MutableStateFlow(false)
    /** Flips true once the server confirms the delete, so the host pops. */
    val deleted: StateFlow<Boolean> = _deleted.asStateFlow()

    private val _actionError = MutableStateFlow<String?>(null)
    /** Non-null while a rename / delete / speaker action failed; shown as a
     *  snackbar. Actions the user explicitly took *do* warrant an error —
     *  unlike the silent enrichment failures above. */
    val actionError: StateFlow<String?> = _actionError.asStateFlow()

    // Bumped by [onRefresh] to re-collect the one-shot detail flow.
    private val revision = MutableStateFlow(0)
    /** Test-only read-only view of [revision]; bumped (0→1→…) by [onRefresh]. */
    internal val revisionValue: StateFlow<Int> get() = revision

    private val _isRefreshing = MutableStateFlow(false)
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    private var autoRefreshJob: Job? = null

    // True between an auto-refresh tick and the first state emission it
    // produces. Ticks are skipped while it is set, so a slow server can't
    // make flatMapLatest cancel each in-flight fetch a second later and
    // starve the screen of updates entirely.
    @Volatile
    private var autoTickPending = false

    private val _youConfirmation = MutableStateFlow<YouConfirmationState>(YouConfirmationState.Idle)
    val youConfirmation: StateFlow<YouConfirmationState> = _youConfirmation.asStateFlow()
    /** Guards against re-prompting within this screen once the user has acted. */
    private var youPromptShown = false

    /** The title as last written from this screen, so a rename shows
     *  immediately rather than waiting for the next fetch. */
    private val localTitle = MutableStateFlow<String?>(null)

    val state: StateFlow<SegmentDetailUiState> = revision
        .flatMapLatest { repo.observeSegment(id) }
        .map { outcome ->
            when (outcome) {
                is Outcome.Failure -> SegmentDetailUiState.Failed(outcome.error.toDisplayMessage())
                is Outcome.Success -> SegmentDetailUiState.Loaded(
                    summary = outcome.value.summary,
                    events = outcome.value.events,
                )
            }
        }
        .onEach { ui ->
            if (_isRefreshing.value) _isRefreshing.value = false
            autoTickPending = false
            (ui as? SegmentDetailUiState.Loaded)?.events?.let {
                seedCacheFromSegment(it)
                maybePromptYouConfirmation(it)
            }
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SegmentDetailUiState.Loading)

    init {
        loadEnrichments()
    }

    /** Memories, waveform and the audio URL. Each failure is logged and left
     *  absent — see the class docs for why none of them is a banner. */
    private fun loadEnrichments() {
        viewModelScope.launch {
            when (val r = repo.memories(id)) {
                is Outcome.Success -> _memories.value = r.value
                is Outcome.Failure -> RecallLog.w(
                    tag = TAG, msg = "memories unavailable: ${r.error.toDisplayMessage()}",
                )
            }
        }
        viewModelScope.launch {
            _waveform.value = (repo.waveform(id) as? Outcome.Success)?.value
        }
        viewModelScope.launch {
            _audio.value = (repo.audioSource(id) as? Outcome.Success)?.value
        }
    }

    fun onRefresh() {
        if (_isRefreshing.value) return
        _isRefreshing.value = true
        revision.value = revision.value + 1
        loadEnrichments()
    }

    /**
     * Start the silent auto-refresh loop (see [AUTO_REFRESH_INTERVAL_MS]):
     * the transcript keeps filling in while the screen is open. Idempotent;
     * the route starts it on `ON_RESUME` and stops it on `ON_PAUSE`.
     */
    fun startAutoRefresh(intervalMs: Long = AUTO_REFRESH_INTERVAL_MS) {
        if (autoRefreshJob?.isActive == true) return
        autoRefreshJob = viewModelScope.launchAutoRefresh(intervalMs) { autoRefreshTick() }
    }

    /** Stop the auto-refresh loop. Safe to call when it isn't running. */
    fun stopAutoRefresh() {
        autoRefreshJob?.cancel()
        autoRefreshJob = null
    }

    /**
     * One silent tick: re-fetch the segment exactly as [onRefresh] does, but
     * without raising [isRefreshing] — no pull-to-refresh spinner appears.
     * Skipped while a manual refresh or a previous tick is still outstanding.
     */
    private fun autoRefreshTick() {
        if (_isRefreshing.value || autoTickPending) return
        autoTickPending = true
        revision.value = revision.value + 1
        loadEnrichments()
    }

    override fun onCleared() {
        stopAutoRefresh()
        super.onCleared()
    }

    /**
     * Rename this recording. The server marks the title user-authored, which
     * permanently protects it from the auto-titler — so this is not a
     * cosmetic edit that the next titling pass will quietly undo.
     */
    fun rename(title: String) {
        val trimmed = title.trim()
        if (trimmed.isEmpty() || trimmed.length > TITLE_MAX_CHARS) return
        localTitle.value = trimmed
        viewModelScope.launch {
            when (val r = repo.rename(id, trimmed)) {
                is Outcome.Success -> {
                    _actionError.value = null
                    revision.value = revision.value + 1
                }
                is Outcome.Failure -> {
                    localTitle.value = null
                    _actionError.value = "Couldn't rename this recording"
                }
            }
        }
    }

    /** The title to show right now: the pending local rename if one is in
     *  flight, else whatever the server last said. */
    fun titleFor(summary: Segment): String = localTitle.value ?: summary.displayTitle

    /**
     * Delete this recording and everything derived from it — transcript,
     * memories, their vectors and the audio.
     *
     * A 409 means the recording is still being written to, which is a
     * "try again when it finishes" answer rather than a failure, and is worth
     * saying in those words.
     */
    fun delete() {
        viewModelScope.launch {
            when (val r = repo.delete(id)) {
                is Outcome.Success -> _deleted.value = true
                is Outcome.Failure -> {
                    _actionError.value = if (r.error.isStillRecording()) {
                        "This is still recording. Try again once it ends."
                    } else {
                        "Couldn't delete this recording"
                    }
                }
            }
        }
    }

    /**
     * Name (or rename) a speaker from the transcript. Optimistically upserts
     * the cache so the label updates immediately, and reverts on failure.
     */
    fun renameSpeaker(speakerId: String, name: String) {
        val prior = speakerCache.get(speakerId)
        val isWearer = prior?.isWearer ?: false
        speakerCache.upsert(speakerId, name, isWearer)
        viewModelScope.launch {
            val sessionId = (state.value as? SegmentDetailUiState.Loaded)
                ?.summary?.sessionId?.value ?: ""
            runCatching { speakerActions.nameSpeaker(sessionId, speakerId, name) }
                .onFailure {
                    RecallLog.e(
                        tag = TAG,
                        msg = "renameSpeaker failed: ${it.javaClass.simpleName}",
                        t = it,
                    )
                    if (prior != null) speakerCache.upsert(speakerId, prior.name, prior.isWearer)
                    else speakerCache.remove(speakerId)
                    _actionError.value = "Couldn't rename on the server"
                }
                .onSuccess { _actionError.value = null }
        }
    }

    /** Reassign a speaker's utterances to another known speaker. */
    fun reassignSpeaker(fromId: String, toId: String) {
        viewModelScope.launch {
            val sessionId = (state.value as? SegmentDetailUiState.Loaded)
                ?.summary?.sessionId?.value ?: ""
            runCatching { speakerActions.reassignSpeaker(sessionId, fromId, toId) }
                .onSuccess { onRefresh() }
                .onFailure {
                    RecallLog.e(
                        tag = TAG,
                        msg = "reassignSpeaker failed: ${it.javaClass.simpleName}",
                        t = it,
                    )
                    _actionError.value = "Couldn't reassign on the server"
                }
        }
    }

    fun dismissActionError() {
        _actionError.value = null
    }

    /**
     * Seed the speaker cache from the loaded transcript so the reassign
     * picker is populated for historical recordings, whose speakers are never
     * seen on the live WS path. Idempotent: a speaker already holding a real
     * name is preserved and never downgraded to null.
     */
    private fun seedCacheFromSegment(events: List<CaptureEvent>) {
        for (chunk in events.filterIsInstance<TranscriptChunk>()) {
            val sid = chunk.speaker ?: continue
            val existing = speakerCache.get(sid)
            if (existing != null && existing.name != null) continue
            speakerCache.upsert(sid, chunk.speakerName, chunk.isWearer)
        }
    }

    /**
     * Surface the one-time wearer-confirmation prompt for the first wearer hop
     * whose name is still unresolved or the placeholder `"You"`.
     */
    private fun maybePromptYouConfirmation(events: List<CaptureEvent>) {
        if (youPromptShown) return
        val wearer = events.filterIsInstance<TranscriptChunk>().firstOrNull { it.isWearer }
            ?: return
        if (wearer.speakerName != null && wearer.speakerName != "You") return
        val wearerId = wearer.speaker ?: return
        val cached = speakerCache.get(wearerId)
        if (cached != null && cached.name != null && cached.name != "You") return
        youPromptShown = true
        _youConfirmation.value = YouConfirmationState.Prompting(wearerId)
    }

    fun confirmYou(chosenName: String) {
        val state = _youConfirmation.value
        if (state !is YouConfirmationState.Prompting) return
        renameSpeaker(state.wearerId, chosenName)
        _youConfirmation.value = YouConfirmationState.Done
    }

    fun dismissYouConfirmation() {
        _youConfirmation.value = YouConfirmationState.Idle
    }

    companion object {
        /** Matches the server's `TITLE_MAX_CHARS`; a longer title is a 400. */
        const val TITLE_MAX_CHARS = 120
        private const val TAG = "SegmentDetail"
    }
}

/** True for the server's "segment is still recording" refusal (409). */
private fun com.openrecall.relay.core.model.ApiError.isStillRecording(): Boolean =
    this is com.openrecall.relay.core.model.ApiError.Http && code == 409
