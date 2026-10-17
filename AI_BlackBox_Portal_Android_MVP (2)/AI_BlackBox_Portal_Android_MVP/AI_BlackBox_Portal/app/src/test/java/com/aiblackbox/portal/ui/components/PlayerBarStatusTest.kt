package com.aiblackbox.portal.ui.components

import com.aiblackbox.portal.ui.components.aurora.AURORA_SILENT_BANDS
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * What the player bar is allowed to SAY while it has nothing to draw.
 *
 * The bar used to fill the gap between "tapped play" and "the envelope decoded" with a synthetic
 * breathing tween — a ribbon that moved to audio it had never measured. Deleting it is only an
 * improvement if something honest replaces it, which is this state machine: the surface reports
 * where it actually is, and the ribbon stays flat until it has real bands.
 */
class PlayerBarStatusTest {

    private fun status(
        active: Boolean = true,
        remote: Boolean = false,
        prepared: Boolean = true,
        envelopeReady: Boolean = true,
        playing: Boolean = true,
        error: Boolean = false,
    ) = playerBarStatus(active, remote, prepared, envelopeReady, playing, error)

    @Test
    fun `a bar for another message is idle, whatever the shared player is doing`() {
        assertEquals(PlayerBarStatus.IDLE, status(active = false, playing = true))
        assertEquals(
            "one clip's failure is not every bar's error",
            PlayerBarStatus.IDLE, status(active = false, error = true),
        )
    }

    @Test
    fun `an error on the active clip is reported`() {
        assertEquals(PlayerBarStatus.ERROR, status(error = true))
    }

    @Test
    fun `a remote clip that is not prepared yet is DOWNLOADING`() {
        assertEquals(PlayerBarStatus.DOWNLOADING, status(remote = true, prepared = false))
    }

    @Test
    fun `a local clip that is not prepared yet is PREPARING`() {
        assertEquals(PlayerBarStatus.PREPARING, status(remote = false, prepared = false))
    }

    @Test
    fun `a prepared clip whose envelope has not decoded is still PREPARING`() {
        // The whole point of the milestone: audio that is ready before its waveform is not "playing".
        assertEquals(PlayerBarStatus.PREPARING, status(envelopeReady = false))
    }

    @Test
    fun `ready and running is PLAYING`() {
        assertEquals(PlayerBarStatus.PLAYING, status())
    }

    @Test
    fun `ready and paused is idle`() {
        assertEquals(PlayerBarStatus.IDLE, status(playing = false))
    }

    @Test
    fun `only PLAYING draws real bands`() {
        val real = floatArrayOf(0.4f, 0.5f, 0.6f, 0.7f)
        assertSame(real, playerRibbonBands(PlayerBarStatus.PLAYING, real))
    }

    @Test
    fun `every other state rests on the SHARED silent bands, never a fresh array`() {
        // Identity, not content: StateFlow and Compose both drop a FloatArray that is the same
        // instance, so a fresh zero array per frame would recompose a resting bar forever.
        val real = floatArrayOf(0.4f, 0.5f, 0.6f, 0.7f)
        for (s in PlayerBarStatus.entries.filter { it != PlayerBarStatus.PLAYING }) {
            assertSame("$s must rest flat", AURORA_SILENT_BANDS, playerRibbonBands(s, real))
        }
    }

    @Test
    fun `http and https urls are remote, file paths are not`() {
        assertTrue(isRemoteAudio("http://box.local/ui/uploads/a.mp3"))
        assertTrue(isRemoteAudio("https://box.local/ui/uploads/a.mp3"))
        assertFalse(isRemoteAudio("/data/user/0/com.aiblackbox.portal/cache/tts_1.mp3"))
        assertFalse(isRemoteAudio(""))
    }
}
