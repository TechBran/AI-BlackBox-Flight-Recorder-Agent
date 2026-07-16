package com.aiblackbox.portal.ui.chat

import android.provider.Settings
import android.os.SystemClock
import androidx.compose.animation.core.tween
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.lazy.LazyListState
import androidx.compose.foundation.gestures.animateScrollBy
import androidx.compose.foundation.gestures.scrollBy
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.input.nestedscroll.NestedScrollConnection
import androidx.compose.ui.input.nestedscroll.NestedScrollSource
import androidx.compose.ui.input.nestedscroll.nestedScroll
import androidx.compose.material3.Icon
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.snapshotFlow
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.layout.boundsInWindow
import androidx.compose.ui.layout.onGloballyPositioned
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.platform.testTag
import androidx.compose.ui.semantics.contentDescription
import androidx.compose.ui.semantics.semantics
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.Dp
import com.aiblackbox.portal.ui.components.SignalLine
import com.aiblackbox.portal.ui.theme.BbxBlack
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlinx.coroutines.withTimeoutOrNull
import kotlin.math.abs

internal const val FOLLOW_RESUME_DELAY_MS = 5_000L

// Completion re-snap grace: the finished reply keeps GROWING after stream_end
// (markdown re-flow, action row, provenance row, and the auto-TTS AudioPlayerBar
// that lands seconds later). Growth inside this window re-triggers the bottom
// glide so the fresh reply stays fully in view — unless the user has scrolled
// since completion (their position always wins).
internal const val COMPLETION_RESNAP_GRACE_MS = 8_000L

// Live-return starvation: how long the RETURNING glide waits for a fresh anchor
// measurement before falling back to the index-free page-to-bottom return (the
// live anchor stops reporting once it is scrolled out of LazyColumn composition).
internal const val RETURN_MEASUREMENT_TIMEOUT_MS = 750L
internal val LIVE_EDGE_GAP = 12.dp
internal val SIGNAL_RESIDENCE_HEIGHT = 40.dp
internal val FALLBACK_COMPOSER_HEIGHT = 180.dp

data class BottomFocalGeometry(
    val residenceTopPx: Float,
    val residenceBottomPx: Float,
    val composerTopPx: Float,
    val composerBottomPx: Float,
    val liveTargetYPx: Float?,
    val occupiedBottomInsetPx: Float = 0f,
    val isReady: Boolean = true,
    private val fallbackAppOwnedBottomClearancePx: Float = 0f,
) {
    val appOwnedBottomClearancePx: Float
        get() {
            val measured = (residenceBottomPx - composerTopPx).coerceAtLeast(0f)
            return if (isReady) measured else maxOf(measured, fallbackAppOwnedBottomClearancePx)
        }
    val bottomClearancePx: Float
        get() = occupiedBottomInsetPx + appOwnedBottomClearancePx
    val returnControlBottomClearancePx: Float
        get() = bottomClearancePx
}

internal fun calculateBottomFocalGeometry(
    windowBottomPx: Float,
    composerTopPx: Float,
    composerBottomPx: Float,
    residenceHeightPx: Float,
    breathingGapPx: Float,
    fallbackComposerHeightPx: Float,
    effectiveBottomInsetPx: Float = 0f,
): BottomFocalGeometry {
    val safeResidenceHeight = residenceHeightPx.coerceAtLeast(0f)
    val safeFallbackComposerHeight = fallbackComposerHeightPx.coerceAtLeast(0f)
    val safeInset = effectiveBottomInsetPx.takeIf { it.isFinite() }?.coerceAtLeast(0f) ?: 0f
    val isReady = windowBottomPx.isFinite() && windowBottomPx > safeInset
    val resolvedWindowBottom = if (isReady) windowBottomPx - safeInset else 0f
    val residenceTop = (resolvedWindowBottom - safeResidenceHeight).coerceAtLeast(0f)
    val hasUsableComposerBounds = composerTopPx.isFinite() &&
        composerBottomPx.isFinite() &&
        composerTopPx <= composerBottomPx &&
        composerBottomPx <= residenceTop
    val resolvedComposerBottom = if (hasUsableComposerBounds) composerBottomPx else residenceTop
    val resolvedComposerTop = if (hasUsableComposerBounds) {
        composerTopPx
    } else {
        (resolvedComposerBottom - safeFallbackComposerHeight).coerceAtLeast(0f)
    }
    return BottomFocalGeometry(
        residenceTopPx = residenceTop,
        residenceBottomPx = resolvedWindowBottom,
        composerTopPx = resolvedComposerTop,
        composerBottomPx = resolvedComposerBottom,
        liveTargetYPx = if (isReady) {
            (resolvedComposerTop - breathingGapPx.coerceAtLeast(0f)).coerceAtLeast(0f)
        } else null,
        occupiedBottomInsetPx = safeInset,
        isReady = isReady,
        fallbackAppOwnedBottomClearancePx = safeFallbackComposerHeight + safeResidenceHeight,
    )
}

internal enum class LiveStreamPhase { IDLE, THINKING, ANSWERING, TOOL }
internal enum class LiveFollowMode { FILLING, FOLLOWING, SUSPENDED, RETURNING, COMPLETED_HISTORY }

internal fun completedHistoryObservation(
    canScrollForward: Boolean,
    mode: LiveFollowMode,
): Pair<Boolean, LiveFollowMode> = canScrollForward to mode

internal data class LiveStreamSnapshot(
    val messageId: String?,
    val reasoningLength: Int,
    val answerLength: Int,
    val phase: LiveStreamPhase,
    val statusLabel: String?,
    // True when the turn ended by CANCEL / ERROR / disconnect rather than a real
    // stream completion — those must keep the viewport where the user put it
    // ("stop cleanly", focal-follow design doc), never run the completion glide.
    val endedAbnormally: Boolean = false,
) {
    val isActive: Boolean get() = phase != LiveStreamPhase.IDLE
    val followKey: Triple<String?, Int, Int>
        get() = Triple(messageId, reasoningLength, answerLength)
}

internal class LiveStreamFollowPolicy {
    var isActive: Boolean = false
        private set
    var mode: LiveFollowMode = LiveFollowMode.FILLING
        private set
    val isSuspended: Boolean get() = mode == LiveFollowMode.SUSPENDED
    var programmaticScroll: Boolean = false
        private set
    var returningToCompletedBottom: Boolean = false
        private set
    private var resumeAtMs: Long? = null

    val showReturnToLive: Boolean
        get() = mode == LiveFollowMode.SUSPENDED ||
            mode == LiveFollowMode.RETURNING ||
            mode == LiveFollowMode.COMPLETED_HISTORY
    val requiresReturnDestination: Boolean get() = showReturnToLive

    fun start() {
        if (!isActive) {
            mode = LiveFollowMode.FILLING
            returningToCompletedBottom = false
        }
        isActive = true
    }

    fun stop() {
        isActive = false
        mode = LiveFollowMode.FILLING
        programmaticScroll = false
        resumeAtMs = null
        returningToCompletedBottom = false
    }

    fun onUserScroll(nowMs: Long) {
        if ((!isActive && !showReturnToLive) || programmaticScroll) return
        if (!isActive) {
            mode = LiveFollowMode.COMPLETED_HISTORY
            resumeAtMs = null
            returningToCompletedBottom = false
            return
        }
        mode = LiveFollowMode.SUSPENDED
        resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onUserScrollSettled(nowMs: Long) {
        if (isSuspended) resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onProgrammaticScrollStarted() { programmaticScroll = true }
    fun onProgrammaticScrollFinished() { programmaticScroll = false }

    fun tick(nowMs: Long): Boolean {
        val deadline = resumeAtMs ?: return false
        if (!isSuspended || nowMs < deadline) return false
        mode = LiveFollowMode.RETURNING
        resumeAtMs = null
        return true
    }

    fun resumeNow(): Boolean {
        if (!isSuspended && mode != LiveFollowMode.COMPLETED_HISTORY) return false
        returningToCompletedBottom = mode == LiveFollowMode.COMPLETED_HISTORY
        mode = LiveFollowMode.RETURNING
        resumeAtMs = null
        return true
    }

    /** Completion snap: when a turn finishes, glide to the TRUE bottom — the fresh
     *  reply (and the TTS bar that follows it) is where the eyes go. Enters the same
     *  index-free completed-bottom return the arrow tap uses, regardless of whether
     *  the user had scrolled away mid-stream. */
    fun completeToBottom() {
        isActive = false
        mode = LiveFollowMode.RETURNING
        returningToCompletedBottom = true
        resumeAtMs = null
    }

    /** A live RETURNING glide starved of measurements converts itself to the
     *  index-free completed-bottom pager (recovery path; no-op outside RETURNING). */
    fun forceCompletedBottomReturn() {
        if (mode == LiveFollowMode.RETURNING) returningToCompletedBottom = true
    }

    /** The bottom pager finished while the stream is still ACTIVE (starvation
     *  recovery): hand control back to the measurement-driven follow. */
    fun resumeFollowingAfterBottomReturn() {
        returningToCompletedBottom = false
        if (mode == LiveFollowMode.RETURNING) {
            mode = if (isActive) LiveFollowMode.FOLLOWING else LiveFollowMode.FILLING
        }
    }

    fun onMeasuredOverflow(overflowPx: Float): Float? = when (mode) {
        LiveFollowMode.FILLING -> if (overflowPx > 0f) {
            mode = LiveFollowMode.FOLLOWING
            overflowPx
        } else null
        LiveFollowMode.FOLLOWING -> overflowPx.takeIf { it > 0f }
        LiveFollowMode.SUSPENDED, LiveFollowMode.RETURNING, LiveFollowMode.COMPLETED_HISTORY -> null
    }

    fun onMeasuredArrival(distancePx: Float, tolerancePx: Float): Boolean {
        if (mode != LiveFollowMode.RETURNING || returningToCompletedBottom || abs(distancePx) > tolerancePx) return false
        mode = if (isActive) LiveFollowMode.FOLLOWING else LiveFollowMode.FILLING
        return true
    }

    fun onCompletedHistoryPosition(canScrollForward: Boolean) {
        if (isActive) return
        if (mode == LiveFollowMode.RETURNING && returningToCompletedBottom) {
            if (!canScrollForward) stop()
            return
        }
        if (canScrollForward) {
            mode = LiveFollowMode.COMPLETED_HISTORY
            resumeAtMs = null
            returningToCompletedBottom = false
        } else if (mode == LiveFollowMode.COMPLETED_HISTORY) {
            stop()
        }
    }

    fun onCompletedReturnStopped(canScrollForward: Boolean) {
        if (isActive || mode != LiveFollowMode.RETURNING || !returningToCompletedBottom) return
        if (!canScrollForward) {
            stop()
        } else {
            mode = LiveFollowMode.COMPLETED_HISTORY
            returningToCompletedBottom = false
        }
    }

    fun onStreamCompleted(hasReturnDestination: Boolean) {
        isActive = false
        when {
            !hasReturnDestination || !showReturnToLive -> stop()
            mode == LiveFollowMode.SUSPENDED -> {
                mode = LiveFollowMode.COMPLETED_HISTORY
                resumeAtMs = null
                returningToCompletedBottom = false
            }
        }
    }
}

internal data class LiveMeasurement(
    val generation: Long,
    val edgeY: Float,
    val targetY: Float,
) {
    val overflowPx: Float get() = edgeY - targetY
}

internal class CompletedBottomAdvanceGuard(
    private val maxSteps: Int = 64,
    private val minimumProgressPx: Float = .5f,
) {
    private var steps = 0
    private var stopped = false

    fun recordProgress(consumedPx: Float): Boolean {
        if (stopped) return false
        steps++
        stopped = !consumedPx.isFinite() || abs(consumedPx) <= minimumProgressPx || steps >= maxSteps
        return !stopped
    }
}

internal class FrameLiveMeasurementConflater {
    private var generation = 0L
    private var edgeY = Float.NaN
    private var targetY = Float.NaN
    private var dirty = false
    private var latest: LiveMeasurement? = null

    fun stageEdge(value: Float) { edgeY = value; dirty = true }
    fun stageTarget(value: Float) { targetY = value; dirty = true }

    fun commitFrame(): LiveMeasurement? {
        if (!dirty || !edgeY.isFinite() || !targetY.isFinite()) return null
        dirty = false
        return LiveMeasurement(++generation, edgeY, targetY).also { latest = it }
    }

    fun consumeAfter(consumedGeneration: Long): LiveMeasurement? =
        latest?.takeIf { it.generation > consumedGeneration }
}

@Stable
internal class LiveStreamFollowState internal constructor(
    val listState: LazyListState,
    private val scope: CoroutineScope,
    private val reducedMotion: () -> Boolean,
    private val nowMs: () -> Long = SystemClock::uptimeMillis,
    private val arrivalTolerancePx: () -> Float = { 1f },
) {
    private val policy = LiveStreamFollowPolicy()
    var edgeY by mutableFloatStateOf(Float.NaN)
        private set
    var targetY by mutableFloatStateOf(Float.NaN)
        private set
    var showReturnToLive by mutableStateOf(false)
        private set
    var requiresReturnDestination by mutableStateOf(false)
        private set
    var completedHistoryObservationMode by mutableStateOf(policy.mode)
        private set
    val returningToLive: Boolean get() = completedHistoryObservationMode == LiveFollowMode.RETURNING

    private val correctionRequests = Channel<Unit>(Channel.CONFLATED)
    private val returnMeasurementRequests = Channel<Unit>(Channel.CONFLATED)
    private val measurements = FrameLiveMeasurementConflater()
    private var measurementCommitJob: Job? = null
    private var correctionJob: Job? = null
    private var returnJob: Job? = null
    private var resumeJob: Job? = null
    private var programmaticLifecycle = 0L
    private var programmaticScrollObserved = false
    private var programmaticScrollFinished = false
    private var hasBeenActive = false
    private var completionGraceUntilMs = 0L
    private var userScrolledSinceCompletion = false

    fun reportEdge(yInWindow: Float) {
        edgeY = yInWindow
        measurements.stageEdge(yInWindow)
        scheduleMeasurementCommit()
    }

    fun setTarget(yInWindow: Float) {
        targetY = yInWindow
        measurements.stageTarget(yInWindow)
        scheduleMeasurementCommit()
    }

    private fun scheduleMeasurementCommit() {
        if (measurementCommitJob?.isActive == true) return
        measurementCommitJob = scope.launch {
            withFrameNanos { }
            if (measurements.commitFrame() != null) {
                correctionRequests.trySend(Unit)
                returnMeasurementRequests.trySend(Unit)
            }
        }
    }

    fun setActive(active: Boolean, glideOnCompletion: Boolean = true) {
        if (active) {
            hasBeenActive = true
            returnJob?.cancel()
            policy.onProgrammaticScrollFinished()
            policy.start()
            ensureCorrectionLoop()
            correctionRequests.trySend(Unit)
        } else if (hasBeenActive) {
            // Stream completion. Land the reply at the TRUE bottom (user report:
            // "that's where your eyes go" — and where the TTS bar appears), even if
            // the user parked upward mid-stream. Two carve-outs: an actively
            // dragging finger is never fought, and an ABNORMAL end (cancel/error/
            // disconnect — glideOnCompletion=false) keeps the viewport in place:
            // a user who taps STOP to read must not have the list yanked away.
            hasBeenActive = false
            resumeJob?.cancel()
            if (!glideOnCompletion ||
                (listState.isScrollInProgress && !policy.programmaticScroll)
            ) {
                policy.onStreamCompleted(hasReturnDestination = edgeY.isFinite())
            } else {
                returnJob?.cancel()
                policy.completeToBottom()
                completionGraceUntilMs = nowMs() + COMPLETION_RESNAP_GRACE_MS
                userScrolledSinceCompletion = false
                startReturn()
            }
        } else {
            // Initial idle composition (no stream ran) — nothing to glide to.
            policy.stop()
            returnJob?.cancel()
            resumeJob?.cancel()
        }
        syncVisibility()
    }

    /** Entry snap: land at the true bottom instantly (history restore / re-entry).
     *  The pre-focal-follow ChatScreen did this via scrollToItem on messages.size;
     *  the follow engine only issues relative corrections, so without this a
     *  restored conversation opens at the TOP of history. */
    fun snapToBottomInstant() {
        scope.launch {
            val last = listState.layoutInfo.totalItemsCount - 1
            if (last >= 0) performProgrammaticScroll { listState.scrollToItem(last, Int.MAX_VALUE) }
        }
    }

    private var lastBootstrapKey: String? = null

    /** New stream: if the live bubble was appended BELOW the composed viewport
     *  (already-filled page, or the user parked up in history), jump to the end so
     *  its anchor enters composition and measurements start flowing — without this
     *  the follow engine can run an entire turn blind (no scroll, no arrow).
     *  Deduped per live MESSAGE: a spurious mid-run inactive→active flip (e.g. one
     *  malformed agent WS frame) re-enters with the same id and must NOT re-yank a
     *  user who parked up in history. */
    fun bootstrapLiveComposition(messageKey: String?) {
        if (messageKey == null || messageKey == lastBootstrapKey) return
        lastBootstrapKey = messageKey
        scope.launch {
            withFrameNanos { }
            if (!policy.isActive) return@launch
            val info = listState.layoutInfo
            val last = info.totalItemsCount - 1
            if (last < 0) return@launch
            val lastVisible = info.visibleItemsInfo.lastOrNull()?.index ?: -1
            if (lastVisible < last) {
                performProgrammaticScroll { listState.scrollToItem(last, Int.MAX_VALUE) }
            }
        }
    }

    fun suspendForUserInput(now: Long = nowMs()) {
        userScrolledSinceCompletion = true
        returnJob?.cancel()
        programmaticLifecycle++
        policy.onProgrammaticScrollFinished()
        policy.onUserScroll(now)
        scheduleResume(now)
        syncVisibility()
    }

    /** True when further user-input deltas of the current gesture add no new
     *  information — lets the nestedScroll hook skip per-frame re-suspension
     *  (each call cancels + relaunches the resume coroutine, so unguarded it
     *  churns at input-frame rate for the whole drag). */
    val userInputLatched: Boolean
        get() = when {
            policy.programmaticScroll -> true
            policy.isActive -> policy.isSuspended && resumeJob?.isActive == true
            else -> policy.mode == LiveFollowMode.COMPLETED_HISTORY
        }

    fun settleUserInput(now: Long = nowMs()) {
        policy.onUserScrollSettled(now)
        scheduleResume(now)
    }

    fun updateCompletedHistoryPosition(canScrollForward: Boolean) {
        if (canScrollForward &&
            !userScrolledSinceCompletion &&
            !policy.isActive &&
            returnJob?.isActive != true &&
            nowMs() < completionGraceUntilMs
        ) {
            // Post-completion growth (action row, provenance, the async TTS bar)
            // inside the grace window with the user untouched — keep the true
            // bottom pinned instead of latching a tap-only arrow.
            policy.completeToBottom()
            startReturn()
            syncVisibility()
            return
        }
        policy.onCompletedHistoryPosition(canScrollForward)
        syncVisibility()
    }

    fun resumeNow() {
        resumeJob?.cancel()
        if (policy.resumeNow()) {
            startReturn()
        } else if (policy.mode == LiveFollowMode.RETURNING && returnJob?.isActive != true) {
            // Belt-and-braces: an arrow tap that lands while the machine reads
            // RETURNING with a DEAD glide job (a cancellation slipped every net)
            // restarts the glide instead of being a silent no-op.
            startReturn()
        }
        syncVisibility()
    }

    fun tick(now: Long = nowMs()) {
        if (policy.tick(now)) startReturn()
        syncVisibility()
    }

    fun correctToTarget() {
        ensureCorrectionLoop()
        correctionRequests.trySend(Unit)
    }

    private fun ensureCorrectionLoop() {
        if (correctionJob?.isActive == true) return
        correctionJob = scope.launch {
            var consumedGeneration = 0L
            for (ignored in correctionRequests) {
                if (!policy.isActive || edgeY.isNaN() || targetY.isNaN()) continue
                val measurement = measurements.consumeAfter(consumedGeneration) ?: continue
                consumedGeneration = measurement.generation
                val overflow = policy.onMeasuredOverflow(measurement.overflowPx) ?: continue
                performProgrammaticScroll { listState.scrollBy(overflow) }
            }
        }
    }

    private fun startReturn() {
        if (returnJob?.isActive == true) return
        while (returnMeasurementRequests.tryReceive().isSuccess) Unit
        returnJob = scope.launch {
            if (policy.returningToCompletedBottom) {
                pageToTrueBottom()
                return@launch
            }
            var consumedGeneration = 0L
            while (policy.mode == LiveFollowMode.RETURNING) {
                val measurement = measurements.consumeAfter(consumedGeneration)
                if (measurement == null) {
                    val got = withTimeoutOrNull(RETURN_MEASUREMENT_TIMEOUT_MS) {
                        returnMeasurementRequests.receive()
                    }
                    if (got == null) {
                        // The live anchor stopped reporting (scrolled out of
                        // composition, or the last step consumed nothing at a list
                        // bound). Recover with the index-free pager — it needs no
                        // measurements and re-composes the anchor at the bottom.
                        policy.forceCompletedBottomReturn()
                        pageToTrueBottom()
                        return@launch
                    }
                    continue
                }
                consumedGeneration = measurement.generation
                val distance = measurement.overflowPx
                if (!distance.isFinite()) return@launch
                if (policy.onMeasuredArrival(distance, tolerancePx = arrivalTolerancePx())) {
                    syncVisibility()
                    correctionRequests.trySend(Unit)
                    return@launch
                }
                performProgrammaticScroll {
                    if (reducedMotion()) listState.scrollBy(distance)
                    else listState.animateScrollBy(distance, tween(durationMillis = 180))
                }
            }
        }
        syncVisibility()
    }

    /** The index-free bottom pager: page viewport-by-viewport, re-measuring after
     *  every layout pass, until the list reports no forward scroll — immune to
     *  index math, oversized items, contentPadding, and growth during the glide.
     *  The epilogue runs in a finally: a DELTA-LESS tap that catches the list
     *  mid-glide mutex-cancels the animateScrollBy (killing this job) without ever
     *  producing a UserInput delta — without the finally, the policy would strand
     *  in RETURNING with a dead job and an arrow whose tap is a no-op. */
    private suspend fun pageToTrueBottom() {
        try {
            val guard = CompletedBottomAdvanceGuard()
            while (policy.mode == LiveFollowMode.RETURNING && listState.canScrollForward) {
                currentCoroutineContext().ensureActive()
                val viewportPx = (listState.layoutInfo.viewportEndOffset -
                    listState.layoutInfo.viewportStartOffset).coerceAtLeast(1).toFloat()
                var consumedPx = 0f
                performProgrammaticScroll {
                    consumedPx = if (reducedMotion()) listState.scrollBy(viewportPx)
                    else listState.animateScrollBy(viewportPx, tween(durationMillis = 120))
                }
                if (!guard.recordProgress(consumedPx)) break
            }
        } finally {
            if (policy.isActive) {
                policy.resumeFollowingAfterBottomReturn()
                correctionRequests.trySend(Unit)
            } else if (policy.mode == LiveFollowMode.RETURNING && policy.returningToCompletedBottom) {
                policy.onCompletedReturnStopped(listState.canScrollForward)
                if (policy.mode == LiveFollowMode.COMPLETED_HISTORY) {
                    // Stopped short (advance-guard wedge or a tap-catch): degrade
                    // ONCE into the tap-only arrow. Clearing the grace window here
                    // is load-bearing — otherwise the guard-stop's own mode flip
                    // re-emits into the grace branch and relaunches the same wedged
                    // glide in a churn loop for the rest of the 8s window.
                    completionGraceUntilMs = 0L
                }
            }
            syncVisibility()
        }
    }

    private suspend fun performProgrammaticScroll(block: suspend () -> Unit) {
        val lifecycle = ++programmaticLifecycle
        programmaticScrollObserved = false
        programmaticScrollFinished = false
        policy.onProgrammaticScrollStarted()
        try {
            block()
        } finally {
            programmaticScrollFinished = true
            if (!listState.isScrollInProgress) finishProgrammaticScroll(lifecycle)
        }
    }

    internal fun onListScrollProgressChanged(scrolling: Boolean) {
        if (policy.programmaticScroll) {
            if (scrolling) {
                programmaticScrollObserved = true
            } else if (programmaticScrollFinished) {
                finishProgrammaticScroll(programmaticLifecycle)
            }
            return
        }
        if (scrolling) suspendForUserInput() else if (showReturnToLive) settleUserInput()
    }

    private fun finishProgrammaticScroll(lifecycle: Long) {
        if (lifecycle != programmaticLifecycle) return
        policy.onProgrammaticScrollFinished()
        programmaticScrollObserved = false
        programmaticScrollFinished = false
    }

    private fun scheduleResume(fromMs: Long) {
        if (!policy.isSuspended) return
        resumeJob?.cancel()
        resumeJob = scope.launch {
            delay(FOLLOW_RESUME_DELAY_MS)
            if (listState.isScrollInProgress) scheduleResume(nowMs())
            else tick(fromMs + FOLLOW_RESUME_DELAY_MS)
        }
    }

    private fun syncVisibility() {
        showReturnToLive = policy.showReturnToLive
        requiresReturnDestination = policy.requiresReturnDestination
        completedHistoryObservationMode = policy.mode
    }
}

@Composable
internal fun rememberLiveStreamFollowState(
    listState: LazyListState,
    snapshot: LiveStreamSnapshot,
    reducedMotionOverride: Boolean? = null,
): LiveStreamFollowState {
    val context = LocalContext.current
    val density = LocalDensity.current
    val scope = rememberCoroutineScope()
    // Read the animator-scale setting ONCE (it's a provider call — reading it on
    // every recomposition means once per streamed token). SignalLine caches the
    // same setting the same way.
    val reducedMotion = remember(reducedMotionOverride) {
        reducedMotionOverride ?: try {
            Settings.Global.getFloat(
                context.contentResolver,
                Settings.Global.ANIMATOR_DURATION_SCALE,
                1f,
            ) == 0f
        } catch (_: Exception) {
            false
        }
    }
    val state = remember(listState, scope) {
        LiveStreamFollowState(
            listState,
            scope,
            reducedMotion = { reducedMotion },
            arrivalTolerancePx = { with(density) { 1.dp.toPx() } },
        )
    }

    LaunchedEffect(state, snapshot.isActive) {
        // endedAbnormally is read, not keyed: it only matters AT the flip, and it
        // derives from the same state change that flips isActive.
        state.setActive(snapshot.isActive, glideOnCompletion = !snapshot.endedAbnormally)
    }
    LaunchedEffect(state, snapshot.isActive, snapshot.messageId) {
        if (snapshot.isActive) state.bootstrapLiveComposition(snapshot.messageId)
    }
    // NOTE: deliberately NOT keyed on state.edgeY/state.targetY — those change on
    // every streamed token, which recomposed the whole caller (and cancelled +
    // relaunched this effect) at layout rate. Corrections are already driven by
    // the frame-conflated measurement commit inside reportEdge/setTarget.
    LaunchedEffect(state, snapshot.followKey, snapshot.phase) {
        state.correctToTarget()
    }
    LaunchedEffect(state, listState) {
        snapshotFlow { listState.isScrollInProgress }
            .distinctUntilChanged()
            .collect { scrolling ->
                state.onListScrollProgressChanged(scrolling)
            }
    }
    LaunchedEffect(state, listState, snapshot.isActive) {
        if (!snapshot.isActive) {
            snapshotFlow {
                completedHistoryObservation(
                    listState.canScrollForward,
                    state.completedHistoryObservationMode,
                )
            }
                .distinctUntilChanged()
                .collect { (canScrollForward, _) ->
                    state.updateCompletedHistoryPosition(canScrollForward)
                }
        }
    }
    return state
}

internal fun Modifier.liveStreamUserInput(followState: LiveStreamFollowState): Modifier =
    nestedScroll(
        object : NestedScrollConnection {
            override fun onPreScroll(available: Offset, source: NestedScrollSource): Offset {
                // Latch: only the FIRST delta of a gesture suspends; the rest of the
                // drag adds nothing (settle refreshes the resume deadline instead).
                if (source == NestedScrollSource.UserInput && !followState.userInputLatched) {
                    followState.suspendForUserInput()
                }
                return Offset.Zero
            }
        },
    )

@Composable
internal fun BoxScope.LiveStreamFocalRail(
    label: String?,
    followState: LiveStreamFollowState,
    modifier: Modifier = Modifier,
    liveTargetYPx: Float? = null,
    effectiveBottomInset: Dp? = null,
) {
    val density = LocalDensity.current
    // Keep the follow target fresh even when the rail itself does not move: the
    // composer can grow (multiline input, attachment strip) without an inset
    // change, which re-computes liveTargetYPx but never re-places the rail.
    LaunchedEffect(followState, liveTargetYPx) {
        liveTargetYPx?.let(followState::setTarget)
    }
    Box(
        modifier = modifier
            .align(Alignment.BottomCenter)
            // Opaque strip behind the Signal line: chat text scrolls visibly behind
            // the TRANSPARENT composer above, but must never bleed into the Signal.
            // background precedes the bottom-inset padding on purpose — the painted
            // rect covers the 40dp strip PLUS the nav-bar/IME inset below it.
            .background(BbxBlack)
            .then(
                if (effectiveBottomInset == null) Modifier.navigationBarsPadding()
                else Modifier.padding(bottom = effectiveBottomInset),
            )
            .fillMaxWidth()
            .height(SIGNAL_RESIDENCE_HEIGHT)
            .testTag("live-stream-rail")
            .semantics { contentDescription = label.orEmpty() }
            .onGloballyPositioned { coordinates ->
                val gapPx = with(density) { LIVE_EDGE_GAP.toPx() }
                followState.setTarget(
                    liveTargetYPx ?: (coordinates.boundsInWindow().top - gapPx),
                )
            },
        contentAlignment = Alignment.Center,
    ) {
        SignalLine(label)
    }
}
