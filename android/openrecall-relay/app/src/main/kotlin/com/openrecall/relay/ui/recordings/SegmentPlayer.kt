package com.openrecall.relay.ui.recordings

import android.content.Context
import android.os.SystemClock
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.platform.LocalContext
import androidx.media3.common.AudioAttributes as Media3AudioAttributes
import androidx.media3.common.C
import androidx.media3.common.MediaItem
import androidx.media3.common.PlaybackException
import androidx.media3.common.Player
import androidx.media3.datasource.DefaultHttpDataSource
import androidx.media3.exoplayer.ExoPlayer
import androidx.media3.exoplayer.source.DefaultMediaSourceFactory
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
 * Uses [ExoPlayer] (Media3) rather than the framework [android.media.MediaPlayer]:
 * the recording audio is Ogg Opus served over HTTP with Range support, and
 * MediaPlayer cannot seek that. It never builds the granule->byte seek map for a
 * remote Ogg stream and reports `getDuration() == 0`, which makes the source
 * unseekable, so `seekTo` is a silent no-op and the playhead snaps back to
 * wherever playback actually was — the "drags the bar but plays from where it
 * started" bug. ExoPlayer's [OggExtractor][androidx.media3.extractor.ogg.OggExtractor]
 * reads the page granule positions, derives the duration from the end-of-stream
 * page and seeks to the matching page over HTTP Range, so scrubbing works.
 *
 * The URL needs the bearer header, which is why the caller passes an
 * [AudioSource] rather than a URL — an unauthenticated request is a 401 and
 * would surface as an opaque "can't play this". The header is set as a default
 * request property on the HTTP data source, so it rides on every Range
 * sub-request the extractor issues to seek, not only the first one.
 */
class SegmentPlayer(private val context: Context) {

    private var player: ExoPlayer? = null

    /** Non-null while a seek is in flight; see [seekTo]. */
    private var seekTargetMs: Long? = null
    /** When that seek was issued, for the [SEEK_TIMEOUT_MS] escape hatch. */
    private var seekIssuedAt: Long = 0

    var state by mutableStateOf(PlaybackState())
        private set

    private val listener = object : Player.Listener {
        override fun onPlaybackStateChanged(playbackState: Int) {
            when (playbackState) {
                Player.STATE_READY -> {
                    // First READY clears the initial "preparing" spinner; we never
                    // re-arm it, so a brief BUFFERING on a mid-playback seek does
                    // not flash the spinner.
                    val d = player?.duration ?: C.TIME_UNSET
                    state = state.copy(
                        preparing = false,
                        durationMs = if (d != C.TIME_UNSET) d else 0L,
                    )
                }
                Player.STATE_ENDED -> {
                    // Park the playhead at the start rather than at the end: the
                    // next tap on play should replay, not sit at a dead stop.
                    state = state.copy(playing = false, positionMs = 0)
                    player?.seekTo(0)
                }
                Player.STATE_IDLE, Player.STATE_BUFFERING -> {
                    // Preparing is armed once in [load] and cleared on READY;
                    // BUFFERING during a seek must not re-arm it.
                }
            }
        }

        override fun onIsPlayingChanged(isPlaying: Boolean) {
            state = state.copy(playing = isPlaying)
        }

        override fun onPlayerError(error: PlaybackException) {
            RecallLog.w(tag = TAG, msg = "playback error code: ${error.errorCode}")
            state = PlaybackState(
                available = false,
                error = "Couldn't play this recording.",
            )
        }
    }

    /**
     * Point the player at a recording. Tears down any previous stream first,
     * so switching recordings can't leave two players talking to the same
     * output.
     */
    fun load(source: AudioSource) {
        release()
        seekTargetMs = null
        state = PlaybackState(available = true, preparing = true)

        // Inject the bearer header on the data source so it is sent on every
        // request, including the Range sub-requests the Ogg extractor issues to
        // seek. setDefaultRequestProperties is the ExoPlayer way to add static
        // headers to an HTTP source.
        val dataSourceFactory = DefaultHttpDataSource.Factory()
            .setUserAgent("OpenRecallRelay")
            .setDefaultRequestProperties(source.headers)
            .setAllowCrossProtocolRedirects(true)
        val mediaSourceFactory =
            DefaultMediaSourceFactory(context).setDataSourceFactory(dataSourceFactory)

        val mp = ExoPlayer.Builder(context)
            .setMediaSourceFactory(mediaSourceFactory)
            .build()
        mp.setAudioAttributes(
            Media3AudioAttributes.Builder()
                .setUsage(C.USAGE_MEDIA)
                .setContentType(C.AUDIO_CONTENT_TYPE_SPEECH)
                .build(),
            /* handleAudioFocus = */ false,
        )
        mp.addListener(listener)
        player = mp
        try {
            mp.setMediaItem(MediaItem.fromUri(source.url))
            mp.prepare()
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
                // onIsPlayingChanged mirrors this, but set it now so the icon
                // flips without waiting on the listener round-trip.
                state = state.copy(playing = false)
            } else {
                mp.play()
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
     * duration only when it has one: with ExoPlayer the Ogg extractor reports a
     * real duration, but on the rare stream where it can't, [MediaPlayer.getDuration]
     * would have returned 0 and gating the seek on it made the scrubber inert on
     * exactly the recordings it was built for — so the caller supplies the
     * position from the duration *it* trusts, and the clamp here is a safety net
     * rather than a precondition.
     */
    fun seekTo(positionMs: Long) {
        val mp = player ?: return
        if (!state.available) return
        val known = state.durationMs
        val target = if (known > 0) positionMs.coerceIn(0L, known) else positionMs.coerceAtLeast(0L)
        // Held until the playhead actually reaches the target. ExoPlayer updates
        // currentPosition to the seek position immediately, but a one-cycle hold
        // keeps the playhead pinned to where the finger was let go during the
        // brief extractor reposition, so the drag reads as continuous rather than
        // fighting the 200 ms position poll.
        seekTargetMs = target
        seekIssuedAt = SystemClock.elapsedRealtime()
        state = state.copy(positionMs = target)
        runCatching {
            mp.seekTo(target)
        }.onFailure {
            seekTargetMs = null
            RecallLog.w(tag = TAG, msg = "seek failed: ${it.javaClass.simpleName}")
        }
    }

    /** Sample the playhead. Called on a timer while playing — ExoPlayer, like
     *  MediaPlayer, has no continuous position callback. */
    fun syncPosition() {
        val mp = player ?: return
        // A seek in flight owns the playhead until the reported position reaches
        // it (ExoPlayer jumps currentPosition to the target on seekTo, so this
        // clears within a cycle). The timeout is an escape hatch against a
        // position that never lands — a playhead frozen on a position the audio
        // never reached is a worse lie than one that admits where it is.
        seekTargetMs?.let { target ->
            val current = runCatching { mp.currentPosition }.getOrDefault(target)
            if (Math.abs(current - target) <= SEEK_LAND_MS ||
                SystemClock.elapsedRealtime() - seekIssuedAt >= SEEK_TIMEOUT_MS
            ) {
                seekTargetMs = null
            } else {
                return
            }
        }
        runCatching {
            if (mp.isPlaying) state = state.copy(positionMs = mp.currentPosition.toLong())
        }
    }

    fun release() {
        player?.let { mp ->
            runCatching {
                mp.removeListener(listener)
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
        /** A seek is considered landed once the playhead is within this of it. */
        const val SEEK_LAND_MS = 400L
    }
}

/**
 * A [SegmentPlayer] bound to the composition: loads [source] when it appears
 * or changes, polls the playhead while playing, and releases the underlying
 * ExoPlayer when the screen leaves. Releasing on dispose is what stops
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