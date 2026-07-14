package com.aiblackbox.portal.ui.chat

import org.junit.Assert.*
import org.junit.Test

class LiveStreamFollowPolicyTest {
    @Test fun `bottom inset is occupied once and clearance includes it`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = 1_000f, effectiveBottomInsetPx = 300f,
            composerTopPx = 400f, composerBottomPx = 640f,
            residenceHeightPx = 60f, breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )
        assertTrue(geometry.isReady)
        assertEquals(640f, geometry.residenceTopPx)
        assertEquals(700f, geometry.residenceBottomPx)
        assertEquals(600f, geometry.bottomClearancePx)
    }

    @Test fun `unmeasured startup has no global live target until geometry is visible`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = Float.NaN, effectiveBottomInsetPx = 300f,
            composerTopPx = Float.NaN, composerBottomPx = Float.NaN,
            residenceHeightPx = 60f, breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )
        assertFalse(geometry.isReady)
        assertNull(geometry.liveTargetYPx)
        assertTrue(geometry.residenceTopPx >= 0f)
    }

    @Test fun `unready geometry reserves fallback composer residence and occupied inset exactly`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = Float.NaN,
            effectiveBottomInsetPx = 300f,
            composerTopPx = Float.NaN,
            composerBottomPx = Float.NaN,
            residenceHeightPx = 60f,
            breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )

        assertNull(geometry.liveTargetYPx)
        assertEquals(260f, geometry.appOwnedBottomClearancePx)
        assertEquals(560f, geometry.bottomClearancePx)
    }
    @Test fun `bottom residence stays below composer controls`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = 1_000f,
            composerTopPx = 700f,
            composerBottomPx = 940f,
            residenceHeightPx = 60f,
            breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )

        assertEquals(940f, geometry.residenceTopPx)
        assertEquals(1_000f, geometry.residenceBottomPx)
        assertTrue(geometry.composerBottomPx <= geometry.residenceTopPx)
    }

    @Test fun `live target sits above composer by breathing gap`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = 1_000f,
            composerTopPx = 700f,
            composerBottomPx = 940f,
            residenceHeightPx = 60f,
            breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )

        assertEquals(688f, geometry.liveTargetYPx)
    }

    @Test fun `unmeasured composer uses safe fallback above reserved residence`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = 1_000f,
            composerTopPx = Float.NaN,
            composerBottomPx = Float.NaN,
            residenceHeightPx = 60f,
            breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )

        assertEquals(740f, geometry.composerTopPx)
        assertEquals(940f, geometry.composerBottomPx)
        assertEquals(728f, geometry.liveTargetYPx)
    }

    @Test fun `unmeasured window keeps local rail fallback active`() {
        val geometry = calculateBottomFocalGeometry(
            windowBottomPx = Float.NaN,
            composerTopPx = Float.NaN,
            composerBottomPx = Float.NaN,
            residenceHeightPx = 60f,
            breathingGapPx = 12f,
            fallbackComposerHeightPx = 200f,
        )

        assertFalse(geometry.isReady)
        assertNull(geometry.liveTargetYPx)
        assertTrue(geometry.residenceTopPx <= geometry.residenceBottomPx)
    }

    @Test fun `user input suspends immediately and resumes only after five idle seconds`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.isSuspended)
        assertTrue(policy.showReturnToLive)
        assertFalse(policy.tick(5_999))
        assertTrue(policy.tick(6_000))
        assertFalse(policy.isSuspended)
    }

    @Test fun `continued interaction resets the five second deadline`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.onUserScrollSettled(4_000)
        assertFalse(policy.tick(8_999))
        assertTrue(policy.tick(9_000))
    }

    @Test fun `down arrow resumes immediately`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.resumeNow())
        assertFalse(policy.isSuspended)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `terminal stream disables delayed return`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.stop()
        assertFalse(policy.tick(20_000))
        assertFalse(policy.isActive)
    }

    @Test fun `programmatic follow never enters suspended state`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onProgrammaticScrollStarted()
        policy.onProgrammaticScrollFinished()
        assertFalse(policy.isSuspended)
    }
}
