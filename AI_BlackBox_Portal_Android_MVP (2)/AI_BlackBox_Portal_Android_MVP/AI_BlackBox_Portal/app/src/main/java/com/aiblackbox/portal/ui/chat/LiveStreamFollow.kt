package com.aiblackbox.portal.ui.chat

import android.provider.Settings
import android.os.SystemClock
import androidx.compose.animation.core.tween
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
import androidx.compose.material3.IconButton
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.KeyboardArrowDown
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.Stable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableFloatStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.rememberUpdatedState
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
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.channels.Channel
import kotlinx.coroutines.currentCoroutineContext
import kotlinx.coroutines.delay
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlin.math.abs

internal const val FOLLOW_RESUME_DELAY_MS = 5_000L
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

    fun setActive(active: Boolean) {
        if (active) {
            returnJob?.cancel()
            policy.onProgrammaticScrollFinished()
            policy.start()
            ensureCorrectionLoop()
            correctionRequests.trySend(Unit)
        } else if (policy.showReturnToLive) {
            policy.onStreamCompleted(hasReturnDestination = edgeY.isFinite())
            if (policy.mode == LiveFollowMode.COMPLETED_HISTORY) resumeJob?.cancel()
        } else {
            policy.stop()
            returnJob?.cancel()
            resumeJob?.cancel()
        }
        syncVisibility()
    }

    fun suspendForUserInput(now: Long = nowMs()) {
        returnJob?.cancel()
        programmaticLifecycle++
        policy.onProgrammaticScrollFinished()
        policy.onUserScroll(now)
        scheduleResume(now)
        syncVisibility()
    }

    fun settleUserInput(now: Long = nowMs()) {
        policy.onUserScrollSettled(now)
        scheduleResume(now)
    }

    fun updateCompletedHistoryPosition(canScrollForward: Boolean) {
        policy.onCompletedHistoryPosition(canScrollForward)
        syncVisibility()
    }

    fun resumeNow() {
        resumeJob?.cancel()
        if (policy.resumeNow()) startReturn()
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
                policy.onCompletedReturnStopped(listState.canScrollForward)
                syncVisibility()
                return@launch
            }
            var consumedGeneration = 0L
            while (policy.mode == LiveFollowMode.RETURNING) {
                val measurement = measurements.consumeAfter(consumedGeneration)
                if (measurement == null) {
                    returnMeasurementRequests.receive()
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
    val reducedMotion by rememberUpdatedState(newValue = reducedMotionOverride ?: try {
        Settings.Global.getFloat(
            context.contentResolver,
            Settings.Global.ANIMATOR_DURATION_SCALE,
            1f,
        ) == 0f
    } catch (_: Exception) {
        false
    })
    val state = remember(listState, scope) {
        LiveStreamFollowState(
            listState,
            scope,
            reducedMotion = { reducedMotion },
            arrivalTolerancePx = { with(density) { 1.dp.toPx() } },
        )
    }

    LaunchedEffect(state, snapshot.isActive) {
        state.setActive(snapshot.isActive)
    }
    LaunchedEffect(state, snapshot.followKey, snapshot.phase, state.edgeY, state.targetY) {
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
                if (source == NestedScrollSource.UserInput) followState.suspendForUserInput()
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
    returnControlBottomClearance: Dp? = null,
    effectiveBottomInset: Dp? = null,
) {
    val density = LocalDensity.current
    Box(
        modifier = modifier
            .align(Alignment.BottomCenter)
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
    if (followState.showReturnToLive) {
        IconButton(
            onClick = followState::resumeNow,
            modifier = Modifier
                .align(Alignment.BottomEnd)
                .then(
                    if (returnControlBottomClearance == null) Modifier.navigationBarsPadding()
                    else Modifier,
                )
                .padding(bottom = returnControlBottomClearance ?: SIGNAL_RESIDENCE_HEIGHT)
                .testTag("return-to-live"),
        ) {
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = "Return to live")
        }
    }
}
