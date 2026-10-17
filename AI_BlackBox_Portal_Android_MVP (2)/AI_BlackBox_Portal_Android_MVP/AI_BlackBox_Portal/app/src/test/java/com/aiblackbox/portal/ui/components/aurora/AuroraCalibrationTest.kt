package com.aiblackbox.portal.ui.components.aurora

import kotlin.math.PI
import kotlin.math.exp
import kotlin.math.sin
import org.junit.Assert.assertArrayEquals
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotEquals
import org.junit.Assert.assertNotSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * CONTAINMENT, which is the whole reason this type exists.
 *
 * Every dynamics constant in [AuroraAnalyser] — the per-band seeds and floors, the live noise
 * floors, HEADROOM, the knee, the AGC time constants — was measured against a stream the
 * platform had already flattened with its own AGC/NS (a Fold 6 speaker-to-mic loop through
 * VOICE_COMMUNICATION). Change the capture source and that entire set is invalidated AT ONCE, not
 * one constant at a time. So the set is a named PROFILE now, and a source that hands the analyser a
 * different kind of stream gets its own slot to be re-measured into.
 *
 * The claims pinned here are the ones a future re-tune must not break by accident:
 *  1. [AuroraCalibration.DEFAULT] IS the shipped calibration, field for field — this refactor moved
 *     the numbers, it did not touch them.
 *  2. The analyser actually READS its profile. A profile nothing consumes is worse than no profile:
 *     it makes a re-tune look done when it changed nothing.
 *  3. [AuroraCalibration.UNPROCESSED] has now been re-measured — the slot was created to be filled,
 *     and what moved when it was filled is pinned so the next change to it is deliberate.
 */
class AuroraCalibrationTest {

    private class FakeClock {
        var ns = 0L
            private set
        val supplier: () -> Long = { ns }
        fun advanceMs(ms: Double) { ns += (ms * 1_000_000.0).toLong() }
    }

    private val rate = 16000
    private val chunkMs = 20.0

    /** One modulated tone per band so every band carries real energy, not leakage. */
    private fun voiceLike(durationMs: Double): ByteArray {
        val n = (rate * durationMs / 1000.0).toInt()
        val out = ByteArray(n * 2)
        for (i in 0 until n) {
            val t = i.toDouble() / rate
            val env = { hz: Double -> 0.35 + 0.65 * (0.5 + 0.5 * sin(2 * PI * hz * t)) }
            val s = 0.18 * env(1.3) * sin(2 * PI * 200 * t) +
                0.16 * env(2.1) * sin(2 * PI * 800 * t) +
                0.12 * env(3.3) * sin(2 * PI * 2600 * t) +
                0.08 * env(4.7) * sin(2 * PI * 5000 * t)
            val v = (s.coerceIn(-1.0, 1.0) * 32767.0).toInt()
            out[2 * i] = (v and 0xFF).toByte()
            out[2 * i + 1] = ((v shr 8) and 0xFF).toByte()
        }
        return out
    }

    /** Feed a second of voice-like audio in [chunkMs] chunks; return every band frame. */
    private fun run(analyser: AuroraAnalyser, clock: FakeClock): List<FloatArray> {
        val chunk = voiceLike(chunkMs)
        val frames = ArrayList<FloatArray>()
        repeat(50) {
            frames.add(analyser.analyze(chunk, chunk.size))
            clock.advanceMs(chunkMs)
        }
        return frames
    }

    // ------------------------------------------------------------ the default IS today

    @Test
    fun `the default profile is the shipped calibration, value for value`() {
        val d = AuroraCalibration.DEFAULT
        assertArrayEquals(AuroraAnalyser.GAINS, d.gains, 0f)
        assertArrayEquals(AuroraAnalyser.PEAK_INIT, d.peakInit, 0f)
        assertArrayEquals(AuroraAnalyser.PEAK_FLOOR, d.peakFloor, 0f)
        assertArrayEquals(AuroraAnalyser.NOISE_LIVE, d.noiseLive, 0f)
        assertEquals(AuroraAnalyser.NOISE_SUBTRACT, d.noiseSubtract, 0f)
        assertEquals(AuroraAnalyser.HEADROOM, d.headroom, 0f)
        assertEquals(AuroraAnalyser.KNEE, d.knee, 0f)
        assertEquals(AuroraAnalyser.RISE_BLEND, d.riseBlend, 0f)
        assertEquals(AuroraAnalyser.ACQUIRE_TAU_SEC, d.acquireTauSec, 0f)
        assertEquals(AuroraAnalyser.STEADY_RISE_TAU_SEC, d.steadyRiseTauSec, 0f)
        assertEquals(AuroraAnalyser.STEADY_DECAY, d.steadyDecay, 0f)
        assertEquals(AuroraAnalyser.STEADY_DECAY_REF_SEC, d.steadyDecayRefSec, 0f)
        assertEquals(AuroraAnalyser.STEADY_DECAY_TAU_SEC, d.steadyDecayTauSec, 0f)
        assertEquals(AuroraAnalyser.FAST_RELEASE_TAU_SEC, d.fastReleaseTauSec, 0f)
    }

    @Test
    fun `the release tau is derived, so it cannot drift from the measured per-chunk decay`() {
        for (p in listOf(AuroraCalibration.DEFAULT, AuroraCalibration.UNPROCESSED)) {
            assertEquals(
                "${p.id}: one ${p.steadyDecayRefSec * 1000f} ms chunk of release",
                p.steadyDecay,
                exp(-p.steadyDecayRefSec / p.steadyDecayTauSec),
                1e-6f,
            )
        }
    }

    @Test
    fun `an analyser with no profile named behaves exactly like one handed the default`() {
        val implicitClock = FakeClock()
        val explicitClock = FakeClock()
        val implicit = run(AuroraAnalyser(rate, implicitClock.supplier), implicitClock)
        val explicit = run(
            AuroraAnalyser(rate, explicitClock.supplier, AuroraCalibration.DEFAULT),
            explicitClock,
        )
        for (i in implicit.indices) assertArrayEquals("frame $i", implicit[i], explicit[i], 0f)
    }

    // ------------------------------------------------------------ the slot for a new source

    /**
     * THIS TEST USED TO ASSERT THE OPPOSITE, AND THAT IS THE POINT OF THE SLOT.
     *
     * It read "the unprocessed slot ... today carries the default numbers", pinning the deliberate
     * decision to wire a new capture source WITHOUT re-measuring the dynamics it feeds, so that the
     * two changes stayed separable and neither could be blamed for the other. That pin has now been
     * cashed: the profile has been re-measured, so the assertions it protected are wrong ON PURPOSE
     * and have been replaced by the ones below, which say what moved and what did not.
     *
     * TWO fields move, and only two — the floor and the depth of the expander that removes it —
     * because they are the only two that describe the SOURCE'S NOISE. The gains, the seeds, the
     * floors of the auto-gain and every time constant describe how loud speech is and how fast it
     * changes, and neither of those becomes a different question when the platform stops processing
     * the stream.
     */
    @Test
    fun `the unprocessed profile has been re-measured — its floor and its expander are its own`() {
        val u = AuroraCalibration.UNPROCESSED
        val d = AuroraCalibration.DEFAULT
        assertNotSame(d, u)
        assertNotEquals(d.id, u.id)

        // WHAT MOVED.
        assertArrayEquals(AuroraCalibration.UNPROCESSED_NOISE_LIVE, u.noiseLive, 0f)
        assertEquals(AuroraCalibration.UNPROCESSED_NOISE_SUBTRACT, u.noiseSubtract, 0f)
        assertFalse(
            "the unprocessed floor is a clone of the processed one again — the derivation is inert",
            d.noiseLive.contentEquals(u.noiseLive),
        )
        // The two floors are shaped by two different noises, and the shape is the claim: pink room
        // tone FALLS with frequency, transducer hiss RISES (and the band gains lift it further), so
        // a floor derived from a room must be dominated by the room at the bottom and by the hiss at
        // the top. The processed guard, measured on an AGC-flattened stream, rises throughout.
        assertTrue(
            "the low band's floor must be the ROOM (${u.noiseLive[0]}), i.e. above the de-inflated " +
                "hiss estimate there",
            u.noiseLive[0] > d.noiseLive[0] / 2.5f,
        )
        assertEquals(
            "the top band's floor must be the HISS — a pink room puts almost nothing there",
            d.noiseLive[3] / 2.5f, u.noiseLive[3], 1e-6f,
        )
        assertTrue(
            "and it subtracts a floor rather than a fraction of a guard, so it subtracts all of it " +
                "(${u.noiseSubtract} vs ${d.noiseSubtract})",
            u.noiseSubtract > d.noiseSubtract,
        )

        // WHAT DID NOT.
        assertArrayEquals(d.gains, u.gains, 0f)
        assertArrayEquals(d.peakInit, u.peakInit, 0f)
        assertArrayEquals(d.peakFloor, u.peakFloor, 0f)
        assertEquals(d.headroom, u.headroom, 0f)
        assertEquals(d.knee, u.knee, 0f)
        assertEquals(d.riseBlend, u.riseBlend, 0f)
        assertEquals(d.acquireTauSec, u.acquireTauSec, 0f)
        assertEquals(d.steadyRiseTauSec, u.steadyRiseTauSec, 0f)
        assertEquals(d.steadyDecay, u.steadyDecay, 0f)
        assertEquals(d.steadyDecayRefSec, u.steadyDecayRefSec, 0f)
        assertEquals(d.fastReleaseTauSec, u.fastReleaseTauSec, 0f)
    }

    /**
     * The floor is DERIVED from the room-tone assumption, not written out beside a comment claiming
     * it was — so moving [AuroraCalibration.UNPROCESSED_ROOM_TONE_DBFS] has to move the floor, or
     * the constant is decoration and the next person to re-measure the room will change nothing.
     *
     * Asserted as a shape rather than as numbers: pink room tone is bass-heavy, so a louder room
     * must lift the LOW band (where the room is what the mic hears) while leaving the top bands on
     * the transducer's own hiss (where the room never was).
     */
    @Test
    fun `the unprocessed floor tracks the room-tone constant it is derived from`() {
        val room = AuroraCalibration.UNPROCESSED_ROOM_TONE_DBFS
        assertTrue("the assumed room must be a real level, was $room", room < 0f && room > -80f)
        // 6 dB is a doubling of amplitude: whatever the derivation is, the low band must follow it.
        val expectedLouder = AuroraCalibration.UNPROCESSED_NOISE_LIVE[0] * 2f
        assertEquals(
            "the low band's floor must double when the assumed room gains 6 dB",
            expectedLouder,
            AuroraCalibration.unprocessedNoiseLiveAt(room + 6f)[0],
            expectedLouder * 0.01f,
        )
        assertTrue(
            "...and the top band must stay on the transducer's hiss, which the room does not set",
            AuroraCalibration.unprocessedNoiseLiveAt(room - 6f)[3] ==
                AuroraCalibration.UNPROCESSED_NOISE_LIVE[3],
        )
    }

    @Test
    fun `a profile is BANDS wide or it is rejected at construction`() {
        for (bad in listOf(floatArrayOf(1f), FloatArray(AuroraAnalyser.BANDS + 1))) {
            var rejected = false
            try {
                AuroraCalibration("bad", gains = bad)
            } catch (_: IllegalArgumentException) {
                rejected = true
            }
            assertTrue("a ${bad.size}-wide array must not build a profile", rejected)
        }
    }

    // ------------------------------------------------------------ the profile is actually read

    @Test
    fun `retuning a profile moves the output — the analyser reads its profile, not the constants`() {
        val baseClock = FakeClock()
        val base = run(AuroraAnalyser(rate, baseClock.supplier), baseClock).last()

        // HEADROOM divides the output ratio, so doubling it must visibly lower every live band.
        // If analyze() were still reading the companion constant this frame would be identical.
        val quietClock = FakeClock()
        val quiet = run(
            AuroraAnalyser(
                rate,
                quietClock.supplier,
                AuroraCalibration("test-headroom", headroom = AuroraAnalyser.HEADROOM * 2f),
            ),
            quietClock,
        ).last()

        for (b in 0 until AuroraAnalyser.BANDS) {
            assertTrue(
                "band $b: ${quiet[b]} must sit below ${base[b]} under double headroom",
                quiet[b] < base[b],
            )
        }
    }

    @Test
    fun `the tap hands its profile to the analyser it owns`() {
        // Same claim one layer up: the capture path builds taps, not analysers, so a profile that
        // stops at the tap boundary would be just as inert as one the analyser ignored.
        val pcm = voiceLike(1000.0)
        fun lastFrame(calibration: AuroraCalibration): FloatArray {
            var last = FloatArray(AuroraAnalyser.BANDS)
            val tap = AudioAnalysisTap(rate, calibration = calibration, nowNs = { 0L })
            tap.offer(pcm, pcm.size) { last = it.bands }
            return last
        }

        val base = lastFrame(AuroraCalibration.DEFAULT)
        val quiet = lastFrame(
            AuroraCalibration("test-headroom", headroom = AuroraAnalyser.HEADROOM * 2f),
        )
        for (b in 0 until AuroraAnalyser.BANDS) {
            assertTrue("band $b: ${quiet[b]} must sit below ${base[b]}", quiet[b] < base[b])
        }
    }
}
