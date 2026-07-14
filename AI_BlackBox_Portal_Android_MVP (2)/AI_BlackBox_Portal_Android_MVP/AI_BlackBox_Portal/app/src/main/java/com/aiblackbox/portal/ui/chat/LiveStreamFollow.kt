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
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlinx.coroutines.yield
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
internal enum class LiveFollowMode { FILLING, FOLLOWING, SUSPENDED, RETURNING }

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
    private var resumeAtMs: Long? = null

    val showReturnToLive: Boolean
        get() = mode == LiveFollowMode.SUSPENDED || mode == LiveFollowMode.RETURNING

    fun start() {
        if (!isActive && !showReturnToLive) mode = LiveFollowMode.FILLING
        isActive = true
    }

    fun stop() {
        isActive = false
        mode = LiveFollowMode.FILLING
        programmaticScroll = false
        resumeAtMs = null
    }

    fun onUserScroll(nowMs: Long) {
        if ((!isActive && !showReturnToLive) || programmaticScroll) return
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
        if (!isSuspended) return false
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
        LiveFollowMode.SUSPENDED, LiveFollowMode.RETURNING -> null
    }

    fun onPhaseChanged() = Unit

    fun onMeasuredArrival(distancePx: Float, tolerancePx: Float): Boolean {
        if (mode != LiveFollowMode.RETURNING || abs(distancePx) > tolerancePx) return false
        mode = LiveFollowMode.FOLLOWING
        return true
    }

    fun onStreamCompleted(hasReturnDestination: Boolean) {
        isActive = false
        if (!hasReturnDestination || !showReturnToLive) stop()
    }
}

@Stable
internal class LiveStreamFollowState internal constructor(
    val listState: LazyListState,
    private val scope: CoroutineScope,
    private val reducedMotion: () -> Boolean,
    private val nowMs: () -> Long = SystemClock::uptimeMillis,
) {
    private val policy = LiveStreamFollowPolicy()
    var edgeY by mutableFloatStateOf(Float.NaN)
        private set
    var targetY by mutableFloatStateOf(Float.NaN)
        private set
    var showReturnToLive by mutableStateOf(false)
        private set

    private var correctionJob: Job? = null
    private var resumeJob: Job? = null
    private var programmaticLifecycle = 0L
    private var programmaticScrollObserved = false
    private var programmaticScrollFinished = false

    fun reportEdge(yInWindow: Float) {
        edgeY = yInWindow
    }

    fun setTarget(yInWindow: Float) {
        targetY = yInWindow
    }

    fun setActive(active: Boolean) {
        if (active) policy.start() else {
            policy.stop()
            correctionJob?.cancel()
            resumeJob?.cancel()
        }
        syncVisibility()
    }

    fun suspendForUserInput(now: Long = nowMs()) {
        correctionJob?.cancel()
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

    fun resumeNow() {
        resumeJob?.cancel()
        if (policy.resumeNow()) correctToTarget()
        syncVisibility()
    }

    fun tick(now: Long = nowMs()) {
        if (policy.tick(now)) correctToTarget()
        syncVisibility()
    }

    fun correctToTarget() {
        if (!policy.isActive || policy.isSuspended || edgeY.isNaN() || targetY.isNaN()) return
        if (abs(edgeY - targetY) < 1f) return

        val previous = correctionJob
        previous?.cancel()
        correctionJob = scope.launch {
            previous?.join()
            if (!policy.isActive || policy.isSuspended) return@launch
            val latestDelta = edgeY - targetY
            if (latestDelta.isNaN() || abs(latestDelta) < 1f) return@launch
            val lifecycle = ++programmaticLifecycle
            programmaticScrollObserved = false
            programmaticScrollFinished = false
            policy.onProgrammaticScrollStarted()
            try {
                if (reducedMotion()) listState.scrollBy(latestDelta)
                else listState.animateScrollBy(latestDelta, tween(durationMillis = 180))
            } finally {
                programmaticScrollFinished = true
                scope.launch {
                    yield()
                    if (
                        lifecycle == programmaticLifecycle &&
                        !programmaticScrollObserved &&
                        !listState.isScrollInProgress
                    ) {
                        finishProgrammaticScroll(lifecycle)
                    }
                }
            }
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
    }
}

@Composable
internal fun rememberLiveStreamFollowState(
    listState: LazyListState,
    snapshot: LiveStreamSnapshot,
    reducedMotionOverride: Boolean? = null,
): LiveStreamFollowState {
    val context = LocalContext.current
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
        LiveStreamFollowState(listState, scope, reducedMotion = { reducedMotion })
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
    returnControlBottomPadding: Dp = SIGNAL_RESIDENCE_HEIGHT,
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
                    if (effectiveBottomInset == null) Modifier.navigationBarsPadding()
                    else Modifier.padding(bottom = effectiveBottomInset),
                )
                .padding(bottom = returnControlBottomPadding)
                .testTag("return-to-live"),
        ) {
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = "Return to live")
        }
    }
}
