package com.openrecall.relay.ui.recordings

import android.content.Context
import android.media.AudioAttributes
import android.media.MediaPlayer
import android.net.Uri
import android.os.SystemClock
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import com.openrecall.relay.core.RecallLog
import com.openrecall.relay.data.AudioSource
import kotlinx.coroutines.delay

/** What the transport controls render. */
data class PlaybackState(
    val available: Boolean = false,
    val preparing: Boolean = false,
    val playing: Boolean = false,
    val positionMs: Long = 0,
    /** The player's own duration once known; 0 until the stream is prepared. */
    val durationMs: Long = 0,
    val error: String? = null,
)

/**
 * Playback of one recording's audio.
 *
 * Uses the framework [MediaPlayer] rather than a media library: this is a
 * single short mono Opus stream with no playlist, no background playback and
 * no notification, and the server serves it with Range support so seeking is
 * already an HTTP concern rather than a buffering one.
 *
 * The URL needs the bearer header, which is why the caller passes an
 * [AudioSource] rather than a URL — an unauthenticated request is a 401 and
 * would surface as an opaque "can't play this".
 */
class SegmentPlayer(private val context: Context) {

    private var player: MediaPlayer? = null

    /** Non-null while a seek is in flight; see [seekTo]. */
    private var seekTargetMs: Long? = null
    /** When that seek was issued, for the [SEEK_TIMEOUT_MS] escape hatch. */
    private var seekIssuedAt: Long = 0

    var state by mutableStateOf(PlaybackState())
        private set

    /**
     * Point the player at a recording. Tears down any previous stream first,
     * so switching recordings can't leave two players talking to the same
     * output.
     */
    fun load(source: AudioSource) {
        release()
        seekTargetMs = null
        state = PlaybackState(available = true, preparing = true)
        val mp = MediaPlayer()
        player = mp
        try {
            mp.setAudioAttributes(
                AudioAttributes.Builder()
                    .setUsage(AudioAttributes.USAGE_MEDIA)
                    .setContentType(AudioAttributes.CONTENT_TYPE_SPEECH)
                    .build(),
            )
            mp.setDataSource(context, Uri.parse(source.url), source.headers)
            mp.setOnPreparedListener { prepared ->
                state = state.copy(
                    preparing = false,
                    durationMs = prepared.duration.toLong().coerceAtLeast(0),
                )
            }
            mp.setOnSeekCompleteListener { seeked ->
                seekTargetMs = null
                // Where it actually landed, which is not necessarily what was
                // asked for — the playhead should tell the truth once it can.
                runCatching {
                    state = state.copy(positionMs = seeked.currentPosition.toLong())
                }
            }
            mp.setOnCompletionListener {
                // Park the playhead at the start rather than at the end: the
                // next tap on play should replay, not sit at a dead stop.
                state = state.copy(playing = false, positionMs = 0)
                runCatching { mp.seekTo(0) }
            }
            mp.setOnErrorListener { _, what, extra ->
                RecallLog.w(tag = TAG, msg = "playback error what=$what extra=$extra")
                state = PlaybackState(
                    available = false,
                    error = "Couldn't play this recording.",
                )
                true
            }
            mp.prepareAsync()
        } catch (e: Exception) {
            RecallLog.w(tag = TAG, msg = "player setup failed: ${e.javaClass.simpleName}")
            state = PlaybackState(available = false, error = "Couldn't play this recording.")
            release()
        }
    }

    fun togglePlayPause() {
        val mp = player ?: return
        if (state.preparing || !state.available) return
        runCatching {
            if (mp.isPlaying) {
                mp.pause()
                state = state.copy(playing = false)
            } else {
                mp.start()
                state = state.copy(playing = true)
            }
        }.onFailure {
            RecallLog.w(tag = TAG, msg = "play/pause failed: ${it.javaClass.simpleName}")
        }
    }

    /**
     * Seek to an absolute position.
     *
     * Absolute rather than fractional, and clamped against the player's own
     * duration only when it has one: [MediaPlayer.getDuration] reports 0 (or
     * -1) for a stream whose container carries no length, which is the normal
     * case for the Opus the relay serves. Gating the seek on that value made
     * the scrubber inert on exactly the recordings it was built for — so the
     * caller supplies the position from the duration *it* trusts, and the
     * clamp here is a safety net rather than a precondition.
     */
    fun seekTo(positionMs: Long) {
        val mp = player ?: return
        if (!state.available) return
        val known = state.durationMs
        val target = if (known > 0) positionMs.coerceIn(0L, known) else positionMs.coerceAtLeast(0L)
        // Held until onSeekComplete. A seek is asynchronous, and until it
        // lands `currentPosition` still reports where playback was *before*
        // it — so the position poll would overwrite the playhead with the old
        // value a fraction of a second later, which reads exactly like the
        // drag having been thrown away.
        seekTargetMs = target
        seekIssuedAt = SystemClock.elapsedRealtime()
        state = state.copy(positionMs = target)
        runCatching {
            // SEEK_CLOSEST rather than plain seekTo(Int), which means
            // SEEK_PREVIOUS_SYNC: this is Ogg Opus with a page per second, so
            // the previous sync point is up to a second behind where the
            // finger was let go, and lands the playhead visibly short.
            mp.seekTo(target, MediaPlayer.SEEK_CLOSEST)
        }.onFailure {
            seekTargetMs = null
            RecallLog.w(tag = TAG, msg = "seek failed: ${it.javaClass.simpleName}")
        }
    }

    /** Sample the playhead. Called on a timer while playing — [MediaPlayer]
     *  has no position callback. */
    fun syncPosition() {
        val mp = player ?: return
        // A seek in flight owns the playhead until it completes. The timeout
        // is an escape hatch: an extractor that quietly ignores a seek never
        // calls back, and a playhead frozen on a position the audio never
        // reached is a worse lie than one that admits where it is.
        seekTargetMs?.let {
            if (SystemClock.elapsedRealtime() - seekIssuedAt < SEEK_TIMEOUT_MS) return
            seekTargetMs = null
        }
        runCatching {
            if (mp.isPlaying) state = state.copy(positionMs = mp.currentPosition.toLong())
        }
    }

    fun release() {
        player?.let { mp ->
            runCatching {
                mp.setOnPreparedListener(null)
                mp.setOnCompletionListener(null)
                mp.setOnSeekCompleteListener(null)
                mp.setOnErrorListener(null)
                mp.reset()
                mp.release()
            }
        }
        player = null
        seekTargetMs = null
    }

    private companion object {
        const val TAG = "SegmentPlayer"
        /** How long a seek may hold the playhead before polling takes it back. */
        const val SEEK_TIMEOUT_MS = 2_000L
    }
}

/**
 * A [SegmentPlayer] bound to the composition: loads [source] when it appears
 * or changes, polls the playhead while playing, and releases the underlying
 * MediaPlayer when the screen leaves. Releasing on dispose is what stops
 * audio when the user navigates back mid-playback.
 */
@Composable
fun rememberSegmentPlayer(source: AudioSource?): SegmentPlayer {
    val context = LocalContext.current
    val player = remember { SegmentPlayer(context.applicationContext) }

    DisposableEffect(source) {
        if (source != null) player.load(source)
        onDispose { player.release() }
    }

    val playing = player.state.playing
    LaunchedEffect(playing) {
        while (playing) {
            player.syncPosition()
            delay(POSITION_POLL_MS)
        }
    }
    return player
}

/** ~5 updates a second: fine enough that the playhead reads as continuous,
 *  coarse enough not to recompose the waveform on every frame. */
private const val POSITION_POLL_MS = 200L
