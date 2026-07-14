package com.aiblackbox.portal.ui.chat

import org.junit.Assert.*
import org.junit.Test

class LiveStreamFollowPolicyTest {
    @Test fun `completed bottom advancement stops after bounded no progress`() {
        val guard = CompletedBottomAdvanceGuard(maxSteps = 8)

        assertTrue(guard.recordProgress(200f))
        assertFalse(guard.recordProgress(0f))
        assertFalse(guard.recordProgress(200f))
    }

    @Test fun `completed bottom advancement is bounded even while making progress`() {
        val guard = CompletedBottomAdvanceGuard(maxSteps = 3)

        assertTrue(guard.recordProgress(1f))
        assertTrue(guard.recordProgress(1f))
        assertFalse(guard.recordProgress(1f))
    }

    @Test fun `completed observer identity changes when position does not`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(canScrollForward = true)
        val historyObservation = completedHistoryObservation(true, policy.mode)

        policy.resumeNow()
        val returningObservation = completedHistoryObservation(true, policy.mode)

        assertNotEquals(historyObservation, returningObservation)
        assertEquals(LiveFollowMode.RETURNING, returningObservation.second)
    }

    @Test fun `blocked completed return remains safely retryable`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(true)
        policy.resumeNow()

        policy.onCompletedReturnStopped(canScrollForward = true)

        assertEquals(LiveFollowMode.COMPLETED_HISTORY, policy.mode)
        assertTrue(policy.showReturnToLive)
        assertTrue(policy.resumeNow())
    }

    @Test fun `production measurements conflate and stale generations cannot be consumed twice`() {
        val pending = FrameLiveMeasurementConflater()
        pending.stageEdge(4f); pending.stageTarget(0f)
        val first = pending.commitFrame()!!
        pending.stageEdge(12f); pending.stageEdge(31f); pending.stageTarget(3f)
        val latest = pending.commitFrame()!!

        assertEquals(latest, pending.consumeAfter(first.generation))
        assertEquals(28f, latest.overflowPx)
        assertNull(pending.consumeAfter(latest.generation))
    }

    @Test fun `measurement generations advance once per distinct frame`() {
        val pending = FrameLiveMeasurementConflater()
        pending.stageEdge(10f); pending.stageTarget(2f)
        val one = pending.commitFrame()!!
        pending.stageEdge(10f)
        val two = pending.commitFrame()!!
        assertEquals(one.generation + 1, two.generation)
    }

    @Test fun `edge and target callbacks coalesce into one latest snapshot per rendered frame`() {
        val pending = FrameLiveMeasurementConflater()
        pending.stageEdge(10f)
        pending.stageTarget(2f)
        pending.stageEdge(31f)
        pending.stageTarget(3f)

        val firstFrame = pending.commitFrame()
        assertEquals(1L, firstFrame?.generation)
        assertEquals(31f, firstFrame?.edgeY)
        assertEquals(3f, firstFrame?.targetY)
        assertNull(pending.commitFrame())

        pending.stageEdge(40f)
        val secondFrame = pending.commitFrame()
        assertEquals(2L, secondFrame?.generation)
        assertEquals(40f, secondFrame?.edgeY)
        assertEquals(3f, secondFrame?.targetY)
    }

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
        assertEquals(600f, geometry.returnControlBottomClearancePx)
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

    @Test fun `stream starts filling and ignores edge before boundary crossing`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()

        assertEquals(LiveFollowMode.FILLING, policy.mode)
        assertNull(policy.onMeasuredOverflow(-24f))
        assertEquals(LiveFollowMode.FILLING, policy.mode)
    }

    @Test fun `first positive overflow starts following and is consumed`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()

        assertEquals(18f, policy.onMeasuredOverflow(18f))
        assertEquals(LiveFollowMode.FOLLOWING, policy.mode)
    }

    @Test fun `thinking to answer handoff retains following mode`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onMeasuredOverflow(10f)

        assertEquals(LiveFollowMode.FOLLOWING, policy.mode)
        assertEquals(6f, policy.onMeasuredOverflow(6f))
    }

    @Test fun `user input suspends immediately and idle expiry enters returning`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertEquals(LiveFollowMode.SUSPENDED, policy.mode)
        assertTrue(policy.showReturnToLive)
        assertFalse(policy.tick(5_999))
        assertTrue(policy.tick(6_000))
        assertEquals(LiveFollowMode.RETURNING, policy.mode)
        assertTrue(policy.showReturnToLive)
    }

    @Test fun `continued interaction resets the five second deadline`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.onUserScrollSettled(4_000)
        assertFalse(policy.tick(8_999))
        assertTrue(policy.tick(9_000))
    }

    @Test fun `down arrow enters returning without hiding arrow`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        assertTrue(policy.resumeNow())
        assertEquals(LiveFollowMode.RETURNING, policy.mode)
        assertTrue(policy.showReturnToLive)
    }

    @Test fun `measured arrival alone resumes following and hides arrow`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.resumeNow()

        assertFalse(policy.onMeasuredArrival(2f, tolerancePx = 1f))
        assertTrue(policy.showReturnToLive)
        assertTrue(policy.onMeasuredArrival(.5f, tolerancePx = 1f))
        assertEquals(LiveFollowMode.FOLLOWING, policy.mode)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `user input interrupts return and restarts idle deadline`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)
        policy.resumeNow()

        policy.onUserScroll(2_000)

        assertEquals(LiveFollowMode.SUSPENDED, policy.mode)
        assertFalse(policy.tick(6_999))
        assertTrue(policy.tick(7_000))
    }

    @Test fun `completion while suspended becomes tap only completed history`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onUserScroll(1_000)

        policy.onStreamCompleted(hasReturnDestination = true)

        assertFalse(policy.isActive)
        assertEquals(LiveFollowMode.COMPLETED_HISTORY, policy.mode)
        assertTrue(policy.showReturnToLive)
        assertFalse(policy.tick(6_000))
        assertTrue(policy.resumeNow())
        assertEquals(LiveFollowMode.RETURNING, policy.mode)
    }

    @Test fun `completed destination is required only through suspended and returning transit`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        assertFalse(policy.requiresReturnDestination)
        policy.onUserScroll(1_000)
        policy.onStreamCompleted(hasReturnDestination = true)
        assertTrue(policy.requiresReturnDestination)
        policy.resumeNow()
        assertTrue(policy.requiresReturnDestination)
        policy.onCompletedHistoryPosition(canScrollForward = false)
        assertFalse(policy.requiresReturnDestination)
    }

    @Test fun `programmatic follow never enters suspended state`() {
        val policy = LiveStreamFollowPolicy()
        policy.start()
        policy.onProgrammaticScrollStarted()
        policy.onProgrammaticScrollFinished()
        assertFalse(policy.isSuspended)
    }

    @Test fun `inactive completed history shows tap only return when newer content remains`() {
        val policy = LiveStreamFollowPolicy()

        policy.onCompletedHistoryPosition(canScrollForward = true)

        assertFalse(policy.isActive)
        assertEquals(LiveFollowMode.COMPLETED_HISTORY, policy.mode)
        assertTrue(policy.showReturnToLive)
        assertTrue(policy.requiresReturnDestination)
    }

    @Test fun `completed history never schedules five second automatic return`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(canScrollForward = true)

        assertFalse(policy.tick(FOLLOW_RESUME_DELAY_MS * 2))
        assertEquals(LiveFollowMode.COMPLETED_HISTORY, policy.mode)
    }

    @Test fun `completed history enters returning only when arrow is tapped`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(canScrollForward = true)

        assertTrue(policy.resumeNow())
        assertEquals(LiveFollowMode.RETURNING, policy.mode)
        assertTrue(policy.showReturnToLive)
    }

    @Test fun `completed history clears shortcut when true bottom is reached manually`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(canScrollForward = true)

        policy.onCompletedHistoryPosition(canScrollForward = false)

        assertEquals(LiveFollowMode.FILLING, policy.mode)
        assertFalse(policy.showReturnToLive)
        assertFalse(policy.requiresReturnDestination)
    }

    @Test fun `new live stream leaves completed history shortcut and starts page fill`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(canScrollForward = true)

        policy.start()

        assertTrue(policy.isActive)
        assertEquals(LiveFollowMode.FILLING, policy.mode)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `completed return hides only at observed true list bottom`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(true)
        policy.resumeNow()
        assertTrue(policy.returningToCompletedBottom)
        policy.onCompletedHistoryPosition(true)
        assertTrue(policy.showReturnToLive)
        policy.onCompletedHistoryPosition(false)
        assertFalse(policy.showReturnToLive)
    }

    @Test fun `user interruption during completed return restores tap only history without timer`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(true)
        policy.resumeNow()
        policy.onUserScroll(2_000)
        assertEquals(LiveFollowMode.COMPLETED_HISTORY, policy.mode)
        assertFalse(policy.tick(20_000))
    }

    @Test fun `new stream cancels completed return and starts filling`() {
        val policy = LiveStreamFollowPolicy()
        policy.onCompletedHistoryPosition(true)
        policy.resumeNow()
        policy.start()
        assertEquals(LiveFollowMode.FILLING, policy.mode)
        assertFalse(policy.returningToCompletedBottom)
    }
}
