package com.sense.relay.ui.recordings

import androidx.lifecycle.ViewModel
import androidx.lifecycle.viewModelScope
import com.sense.relay.core.SenseLog
import com.sense.relay.core.result.Outcome
import com.sense.relay.core.ui.toDisplayMessage
import com.sense.relay.data.NoopSpeakerActions
import com.sense.relay.data.SpeakerActions
import com.sense.relay.data.SpeakerCache
import com.sense.relay.data.SessionRepository
import com.sense.relay.domain.model.CaptureEvent
import com.sense.relay.domain.model.SessionDetails
import com.sense.relay.domain.model.SessionId
import com.sense.relay.domain.model.SessionSummary
import com.sense.relay.domain.model.TranscriptChunk
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.SharingStarted
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.combine
import kotlinx.coroutines.flow.flatMapLatest
import kotlinx.coroutines.flow.onEach
import kotlinx.coroutines.flow.onStart
import kotlinx.coroutines.flow.stateIn
import kotlinx.coroutines.launch

/**
 * What the SessionDetail screen renders. Progressive: the summary lands
 * first ([LoadedSummary]), then the event timeline fills in ([Loaded]).
 * Sealed for exhaustive `when` rendering.
 */
sealed interface SessionDetailUiState {
    data object Loading : SessionDetailUiState
    data class LoadedSummary(val summary: SessionSummary) : SessionDetailUiState
    data class Loaded(
        val summary: SessionSummary,
        val events: List<CaptureEvent>,
    ) : SessionDetailUiState
    data class Failed(val reason: String) : SessionDetailUiState
}

/**
 * One-time "is this you? what should I call you?" prompt for the wearer.
 *
 * - [Idle]: nothing to show (no wearer seen yet, or already named).
 * - [Prompting]: the first transcript hop flagged `isWearer` whose resolved
 *   name is still null or the placeholder `"You"`. Carries the wearer's
 *   stable speaker id so [confirmYou] knows who to rename.
 * - [Done]: the user confirmed a name; the prompt won't re-show this session.
 *
 * Session-scoped (not persisted): if the app restarts while the wearer is
 * still called "You", the prompt re-shows — the right behavior for a
 * confirmation the user might have dismissed accidentally.
 */
sealed interface YouConfirmationState {
    data object Idle : YouConfirmationState
    data class Prompting(val wearerId: String) : YouConfirmationState
    data object Done : YouConfirmationState
}

/**
 * SessionDetail ViewModel. `combine`s the summary fetch with the event-list
 * fetch, but seeds the events flow with a `null` via [onStart] so the state
 * can advance to [SessionDetailUiState.LoadedSummary] the moment the summary
 * arrives — without waiting for the (potentially larger) event list. When
 * the events arrive it advances to [SessionDetailUiState.Loaded].
 *
 * Error policy: a summary failure is [SessionDetailUiState.Failed] (nothing
 * to show); an events failure keeps [SessionDetailUiState.LoadedSummary] (the
 * summary still renders, the timeline just stays empty).
 *
 * Refresh: the underlying per-session flows are one-shot cold flows (each
 * `GET` emits once). [onRefresh] bumps a [revision] trigger that
 * [flatMapLatest] uses to cancel the in-flight collection and re-collect the
 * two flows from scratch — re-fetching summary + events. The [isRefreshing]
 * flag is cleared on the new collection's first emission (summary landed) so
 * the pull-to-refresh spinner dismisses as soon as the refresh produces a
 * new state.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class SessionDetailViewModel(
    private val id: SessionId,
    private val repo: SessionRepository,
    val speakerCache: SpeakerCache = SpeakerCache(),
    private val speakerActions: SpeakerActions = NoopSpeakerActions,
) : ViewModel() {

    // Bumped by [onRefresh] to re-collect the one-shot per-session flows.
    private val revision = MutableStateFlow(0)

    private val _isRefreshing = MutableStateFlow(false)
    /** True while a pull-to-refresh re-fetch is in flight. */
    val isRefreshing: StateFlow<Boolean> = _isRefreshing.asStateFlow()

    private val _speakerError = MutableStateFlow<String?>(null)
    /** Non-null while a rename/reassign HTTP call failed; the banner shows it. */
    val speakerError: StateFlow<String?> = _speakerError.asStateFlow()

    private val _youConfirmation = MutableStateFlow<YouConfirmationState>(YouConfirmationState.Idle)
    /** One-time wearer-confirmation prompt state; see [YouConfirmationState]. */
    val youConfirmation: StateFlow<YouConfirmationState> = _youConfirmation.asStateFlow()
    /** Guards against re-prompting within this session once the user has acted. */
    private var youPromptShown = false

    val state: StateFlow<SessionDetailUiState> = revision
        .flatMapLatest {
            // Flow is covariant, so a non-null events flow binds to a nullable-
            // typed one; `onStart { emit(null) }` then seeds a "pending" tick so
            // `combine` fires as soon as the summary lands (events still null
            // → LoadedSummary). The explicit nullable element type MUST be on
            // the bare flow before `onStart` — inlining drops the annotation,
            // `onStart` infers a non-null element, and `emit(null)` won't compile.
            val eventsOrPending: Flow<Outcome<List<CaptureEvent>>?> = repo.observeSessionEvents(id)
            combine(
                repo.observeSession(id),
                eventsOrPending.onStart { emit(null) },
            ) { summary, events -> reduce(summary, events) }
                // Clear the refresh flag on the first emission of this (re-)
                // collection — the summary fetch has landed, the spinner can
                // dismiss. A no-op for the initial collection (flag is false).
                // Also scan the loaded events for a wearer needing confirmation.
                .onEach { ui ->
                    if (_isRefreshing.value) _isRefreshing.value = false
                    (ui as? SessionDetailUiState.Loaded)?.events?.let { maybePromptYouConfirmation(it) }
                }
        }
        .stateIn(viewModelScope, SharingStarted.WhileSubscribed(5_000), SessionDetailUiState.Loading)

    /**
     * Pull-to-refresh: re-fetch the session summary + event timeline. Bumps
     * [revision] so [flatMapLatest] cancels the current collection and re-
     * collects the one-shot flows from scratch. No-op if a refresh is already
     * in flight.
     */
    fun onRefresh() {
        if (_isRefreshing.value) return
        _isRefreshing.value = true
        revision.value = revision.value + 1
    }

    /**
     * Name (or rename) a speaker from the Recordings timeline. Calls the
     * HTTP rename endpoint; the server's `set_display_name` applies, and the
     * next transcript §E / `/speakers` refresh carries the new name.
     * Optimistically upserts the cache so the label updates immediately, and
     * reverts the cache + surfaces [speakerError] on a send failure.
     */
    fun renameSpeaker(speakerId: String, name: String) {
        val prior = speakerCache.get(speakerId)
        val isWearer = prior?.isWearer ?: false
        speakerCache.upsert(speakerId, name, isWearer)
        viewModelScope.launch {
            runCatching { speakerActions.nameSpeaker(id.value, speakerId, name) }
                .onFailure {
                    SenseLog.e(tag = "SessionDetail", msg = "renameSpeaker failed: ${it.javaClass.simpleName}", t = it)
                    if (prior != null) speakerCache.upsert(speakerId, prior.name, prior.isWearer)
                    else speakerCache.remove(speakerId)
                    _speakerError.value = "Couldn't rename on the server"
                }
                .onSuccess { _speakerError.value = null }
        }
    }

    /**
     * Reassign a speaker's utterances to another known speaker. v1 uses
     * `scope="all"` (the server is session-scoped, so "all of this speaker" is
     * "this conversation"). On success re-fetches the session so the labels
     * refresh; on failure surfaces [speakerError].
     */
    fun reassignSpeaker(fromId: String, toId: String) {
        viewModelScope.launch {
            runCatching { speakerActions.reassignSpeaker(id.value, fromId, toId) }
                .onSuccess { onRefresh() }
                .onFailure {
                    SenseLog.e(tag = "SessionDetail", msg = "reassignSpeaker failed: ${it.javaClass.simpleName}", t = it)
                    _speakerError.value = "Couldn't reassign on the server"
                }
        }
    }

    /** Clear the speaker error banner (dismissed by the user). */
    fun dismissSpeakerError() {
        _speakerError.value = null
    }

    /**
     * Scan a freshly loaded event batch for the first wearer hop whose name
     * is still unresolved (null) or the placeholder `"You"`, and surface the
     * one-time confirmation prompt. Idempotent within a session via
     * [youPromptShown]; skipped if the cache already holds a real name for
     * the wearer (e.g. the user renamed them in a prior view).
     */
    private fun maybePromptYouConfirmation(events: List<CaptureEvent>) {
        if (youPromptShown) return
        val wearer = events.filterIsInstance<TranscriptChunk>().firstOrNull { chunk -> chunk.isWearer }
            ?: return
        if (wearer.speakerName != null && wearer.speakerName != "You") return
        val wearerId = wearer.speaker ?: return
        val cached = speakerCache.get(wearerId)
        if (cached != null && cached.name != null && cached.name != "You") return
        youPromptShown = true
        _youConfirmation.value = YouConfirmationState.Prompting(wearerId)
    }

    /**
     * Confirm the wearer's name. Sends `name_speaker` (via [renameSpeaker],
     * which optimistically updates the cache) and flips the prompt to
     * [YouConfirmationState.Done]. No-op unless the prompt is showing.
     */
    fun confirmYou(chosenName: String) {
        val state = _youConfirmation.value
        if (state !is YouConfirmationState.Prompting) return
        renameSpeaker(state.wearerId, chosenName)
        _youConfirmation.value = YouConfirmationState.Done
    }

    /** Dismiss the prompt without naming (it will re-show after a restart). */
    fun dismissYouConfirmation() {
        _youConfirmation.value = YouConfirmationState.Idle
    }
}

private fun reduce(
    summary: Outcome<SessionDetails>,
    events: Outcome<List<CaptureEvent>>?,
): SessionDetailUiState = when (summary) {
    is Outcome.Failure -> SessionDetailUiState.Failed(summary.error.toDisplayMessage())
    is Outcome.Success -> {
        val s = summary.value.summary
        when (events) {
            null -> SessionDetailUiState.LoadedSummary(s) // events still pending
            is Outcome.Failure -> SessionDetailUiState.LoadedSummary(s) // events failed; summary stands
            is Outcome.Success -> SessionDetailUiState.Loaded(s, events.value)
        }
    }
}
