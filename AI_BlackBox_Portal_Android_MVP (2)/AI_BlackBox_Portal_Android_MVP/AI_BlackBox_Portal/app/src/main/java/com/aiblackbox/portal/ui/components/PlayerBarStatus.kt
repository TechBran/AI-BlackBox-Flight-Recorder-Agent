package com.aiblackbox.portal.ui.components

import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS

/**
 * What the player bar is DOING, as one value derived from the playback manager's flags.
 *
 * It exists because the bar used to have no way to say "I have audio but not its waveform yet", and
 * filled that gap with a synthetic breathing tween — a ribbon that moved to audio it had never
 * measured. The tween is gone; this is what replaced it. A surface that reports its real state is
 * the only honest thing to draw while there is nothing to draw.
 */
enum class PlayerBarStatus {
    /** Nothing to say: another message's bar, or this one prepared and paused. */
    IDLE,

    /** A remote clip whose bytes are still arriving. */
    DOWNLOADING,

    /** Bytes in hand, waveform not yet: the decoder (or the player) is still working. */
    PREPARING,

    /** Audio out, real bands on the ribbon. The only state that draws a moving waveform. */
    PLAYING,

    /** This clip failed. */
    ERROR,
}

/**
 * ORDER IS THE CONTRACT. `active` first, because [com.aiblackbox.portal.data.voice.AudioPlaybackManager]
 * is a singleton and its error/playing flags describe ONE clip — without this gate every bar in the
 * thread would light up red when any single clip failed. Then the error, then the two waits, and
 * only a bar that is prepared AND has its envelope may call itself playing.
 */
internal fun playerBarStatus(
    active: Boolean,
    remote: Boolean,
    prepared: Boolean,
    envelopeReady: Boolean,
    playing: Boolean,
    error: Boolean,
): PlayerBarStatus = when {
    !active -> PlayerBarStatus.IDLE
    error -> PlayerBarStatus.ERROR
    !prepared -> if (remote) PlayerBarStatus.DOWNLOADING else PlayerBarStatus.PREPARING
    !envelopeReady -> PlayerBarStatus.PREPARING
    playing -> PlayerBarStatus.PLAYING
    else -> PlayerBarStatus.IDLE
}

/** A url the player has to fetch before it can prepare, as opposed to a file already on disk. */
internal fun isRemoteAudio(url: String): Boolean =
    url.startsWith("http://") || url.startsWith("https://")

/**
 * The bands the ribbon draws: the measured ones while playing, and the SHARED silent array in every
 * other state.
 *
 * Shared, not a fresh `FloatArray(4)`: Compose and `StateFlow` both compare a `FloatArray` by
 * identity, so returning a new zero array per frame would recompose every resting bar in the thread
 * at the tick rate. See AURORA_SILENT_BANDS.
 */
internal fun playerRibbonBands(status: PlayerBarStatus, measured: FloatArray): FloatArray =
    if (status == PlayerBarStatus.PLAYING) measured else AURORA_SILENT_BANDS
