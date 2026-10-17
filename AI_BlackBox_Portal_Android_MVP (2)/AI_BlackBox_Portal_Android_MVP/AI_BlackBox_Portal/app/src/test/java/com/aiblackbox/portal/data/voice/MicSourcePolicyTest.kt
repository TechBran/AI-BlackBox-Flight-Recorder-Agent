package com.aiblackbox.portal.data.voice

import android.media.MediaRecorder
import com.aiblackbox.portal.ui.components.aurora.AuroraCalibration
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The mic-source decision, in the only place it can be held: a pure function of the ONE answer the
 * platform gives.
 *
 * WHY THIS IS NOT A TRY/CATCH LADDER. `UNPROCESSED` FAILS SILENTLY — AOSP's contract is that a
 * device without it "behaves like DEFAULT otherwise", so opening it always succeeds and an
 * exception-driven or state-driven ladder (OverlayService's recording path is exactly that shape)
 * can never tell a real unprocessed stream from a processed one wearing its name. The only signal
 * that exists is `AudioManager.PROPERTY_SUPPORT_AUDIO_SOURCE_UNPROCESSED`, and it is a STRING.
 *
 * Everything below the first entry is a different question — a source that will not OPEN at all is
 * a hard failure with a state to check, and falling past it is legitimate. Keep the two apart: the
 * capability answer decides whether UNPROCESSED is in the chain, never the outcome of trying it.
 */
class MicSourcePolicyTest {

    // ----------------------------------------------------------------- capability

    @Test
    fun `unprocessed support is the platform property string, and only that`() {
        assertTrue(MicSourcePolicy.supportsUnprocessed("true"))
        assertTrue("the property is documented case-insensitively", MicSourcePolicy.supportsUnprocessed("TRUE"))
        assertTrue(MicSourcePolicy.supportsUnprocessed("True"))

        // Absent property (the common case on devices that never implemented it) reads as NO.
        assertFalse(MicSourcePolicy.supportsUnprocessed(null))
        assertFalse(MicSourcePolicy.supportsUnprocessed(""))
        assertFalse(MicSourcePolicy.supportsUnprocessed("false"))
        // Not "any non-empty answer": a device answering something else has not said yes.
        assertFalse(MicSourcePolicy.supportsUnprocessed("1"))
        assertFalse(MicSourcePolicy.supportsUnprocessed("yes"))
    }

    // ----------------------------------------------------------------- the chain

    @Test
    fun `a device that says yes gets UNPROCESSED first, then the processed fallbacks`() {
        assertEquals(
            listOf(MicSource.UNPROCESSED, MicSource.VOICE_RECOGNITION, MicSource.MIC),
            MicSourcePolicy.simplexChain(unprocessedSupported = true),
        )
    }

    @Test
    fun `a device that does not say yes never attempts UNPROCESSED at all`() {
        val chain = MicSourcePolicy.simplexChain(unprocessedSupported = false)
        assertEquals(listOf(MicSource.VOICE_RECOGNITION, MicSource.MIC), chain)
        assertFalse("a silent no-op source must never be opened blind", MicSource.UNPROCESSED in chain)
    }

    @Test
    fun `the duplex source is not in the simplex chain`() {
        // VOICE_COMMUNICATION exists to be echo-cancelled against a speaker that is playing. The
        // composer mic is simplex — nothing is playing — so it must not pay that processing.
        for (supported in listOf(true, false)) {
            assertFalse(MicSource.VOICE_COMMUNICATION in MicSourcePolicy.simplexChain(supported))
        }
        assertSame(MicSource.VOICE_COMMUNICATION, MicSourcePolicy.DUPLEX_SOURCE)
    }

    // ----------------------------------------------------------------- opening

    @Test
    fun `openFirst takes the first source that opens and reports which one that was`() {
        val tried = ArrayList<MicSource>()
        val opened = MicSourcePolicy.openFirst(MicSourcePolicy.simplexChain(true)) {
            tried.add(it)
            "record"
        }
        assertEquals(MicSource.UNPROCESSED, opened?.source)
        assertEquals("record", opened?.record)
        assertEquals("nothing past the first success may be opened", listOf(MicSource.UNPROCESSED), tried)
    }

    @Test
    fun `openFirst walks past a source that will not open, in order`() {
        val tried = ArrayList<MicSource>()
        val opened = MicSourcePolicy.openFirst(MicSourcePolicy.simplexChain(true)) {
            tried.add(it)
            if (it == MicSource.MIC) "record" else null
        }
        assertEquals(MicSource.MIC, opened?.source)
        assertEquals(
            listOf(MicSource.UNPROCESSED, MicSource.VOICE_RECOGNITION, MicSource.MIC),
            tried,
        )
    }

    @Test
    fun `openFirst reports nothing rather than a half-open mic when every source fails`() {
        assertNull(MicSourcePolicy.openFirst(MicSourcePolicy.simplexChain(false)) { null })
    }

    // ----------------------------------------------------------------- the table

    @Test
    fun `every source names the platform constant it opens`() {
        assertEquals(MediaRecorder.AudioSource.UNPROCESSED, MicSource.UNPROCESSED.androidSource)
        assertEquals(MediaRecorder.AudioSource.VOICE_RECOGNITION, MicSource.VOICE_RECOGNITION.androidSource)
        assertEquals(MediaRecorder.AudioSource.MIC, MicSource.MIC.androidSource)
        assertEquals(MediaRecorder.AudioSource.VOICE_COMMUNICATION, MicSource.VOICE_COMMUNICATION.androidSource)
        assertEquals(
            "each source opens a distinct platform source",
            MicSource.entries.size,
            MicSource.entries.map { it.androidSource }.distinct().size,
        )
    }

    @Test
    fun `only the duplex source attaches an echo canceller, and nothing attaches a noise suppressor`() {
        // AEC is not optional on the duplex path: the voice screen's mic is open while the model is
        // speaking through the loudspeaker, so dropping it means the model transcribes itself on
        // barge-in. NS is a different bargain — it reshapes the very spectrum the ribbon draws, and
        // nothing on any path depends on it.
        for (source in MicSource.entries) {
            val effects = MicSourcePolicy.effectsFor(source)
            assertEquals(
                "${source.name} echo canceller",
                source == MicSourcePolicy.DUPLEX_SOURCE,
                effects.echoCanceler,
            )
            assertFalse("${source.name} noise suppressor", effects.noiseSuppressor)
        }
    }

    @Test
    fun `only the unprocessed source leaves the default calibration profile`() {
        // The containment rule: every measured analyser constant was calibrated against a stream the
        // platform had already AGC-flattened, so the source that bypasses that processing is the one
        // that cannot inherit them. See AuroraCalibrationTest.
        for (source in MicSource.entries) {
            val expected =
                if (source == MicSource.UNPROCESSED) AuroraCalibration.UNPROCESSED
                else AuroraCalibration.DEFAULT
            assertSame("${source.name} calibration", expected, source.calibration)
        }
    }

    // ── W10 mic A/B: a debug build can pin the simplex mic (gradle -PforceMicSource=NAME) ──

    @Test
    fun `a debug A-B build can pin the simplex mic to one source (W10)`() {
        assertEquals(
            listOf(MicSource.VOICE_COMMUNICATION, MicSource.VOICE_RECOGNITION, MicSource.MIC),
            MicSourcePolicy.simplexChain(unprocessedSupported = true, forcedSource = "VOICE_COMMUNICATION"),
        )
        assertEquals(
            listOf(MicSource.MIC, MicSource.VOICE_RECOGNITION),
            MicSourcePolicy.simplexChain(unprocessedSupported = false, forcedSource = "MIC"),
        )
    }

    @Test
    fun `an empty or unknown pin leaves the shipped chain untouched`() {
        for (supported in listOf(true, false)) {
            val shipped = MicSourcePolicy.simplexChain(supported, forcedSource = "")
            assertEquals(shipped, MicSourcePolicy.simplexChain(supported, forcedSource = "NOT_A_SOURCE"))
        }
        assertEquals(
            listOf(MicSource.UNPROCESSED, MicSource.VOICE_RECOGNITION, MicSource.MIC),
            MicSourcePolicy.simplexChain(unprocessedSupported = true, forcedSource = ""),
        )
    }

    @Test
    fun `a normal build pins nothing`() {
        // The pin exists only for the W10 A/B APK. If this fails, the gate is running on an
        // A/B build (-PforceMicSource was passed), which is not a build that may ship.
        assertEquals("", com.aiblackbox.portal.BuildConfig.FORCE_MIC_SOURCE)
        assertEquals(
            MicSourcePolicy.simplexChain(unprocessedSupported = true, forcedSource = ""),
            MicSourcePolicy.simplexChain(unprocessedSupported = true),
        )
    }
}
