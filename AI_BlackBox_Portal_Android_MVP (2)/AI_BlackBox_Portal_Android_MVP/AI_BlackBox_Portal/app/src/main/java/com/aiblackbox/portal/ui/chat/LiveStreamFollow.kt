package com.aiblackbox.portal.ui.chat

import android.provider.Settings
import android.os.SystemClock
import androidx.compose.animation.core.tween
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.BoxScope
import androidx.compose.foundation.layout.offset
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
import androidx.compose.ui.unit.dp
import com.aiblackbox.portal.ui.components.SignalLine
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.delay
import kotlinx.coroutines.flow.distinctUntilChanged
import kotlinx.coroutines.launch
import kotlinx.coroutines.yield
import kotlin.math.abs

internal const val FOLLOW_RESUME_DELAY_MS = 5_000L
internal val FOCAL_RAIL_OFFSET = 36.dp
internal val LIVE_EDGE_GAP = 12.dp

internal enum class LiveStreamPhase { IDLE, THINKING, ANSWERING, TOOL }

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
    var isSuspended: Boolean = false
        private set
    var programmaticScroll: Boolean = false
        private set
    private var resumeAtMs: Long? = null

    val showReturnToLive: Boolean get() = isActive && isSuspended

    fun start() { isActive = true }

    fun stop() {
        isActive = false
        isSuspended = false
        programmaticScroll = false
        resumeAtMs = null
    }

    fun onUserScroll(nowMs: Long) {
        if (!isActive || programmaticScroll) return
        isSuspended = true
        resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onUserScrollSettled(nowMs: Long) {
        if (isSuspended) resumeAtMs = nowMs + FOLLOW_RESUME_DELAY_MS
    }

    fun onProgrammaticScrollStarted() { programmaticScroll = true }
    fun onProgrammaticScrollFinished() { programmaticScroll = false }

    fun tick(nowMs: Long): Boolean {
        val deadline = resumeAtMs ?: return false
        if (!isActive || !isSuspended || nowMs < deadline) return false
        isSuspended = false
        resumeAtMs = null
        return true
    }

    fun resumeNow(): Boolean {
        if (!isActive || !isSuspended) return false
        isSuspended = false
        resumeAtMs = null
        return true
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
): LiveStreamFollowState {
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val reducedMotion by rememberUpdatedState(newValue = try {
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
) {
    val density = LocalDensity.current
    Box(
        modifier = modifier
            .align(Alignment.Center)
            .offset(y = FOCAL_RAIL_OFFSET)
            .testTag("live-stream-rail")
            .onGloballyPositioned { coordinates ->
                val gapPx = with(density) { LIVE_EDGE_GAP.toPx() }
                followState.setTarget(coordinates.boundsInWindow().top - gapPx)
            },
    ) {
        SignalLine(label)
    }
    if (followState.showReturnToLive) {
        IconButton(
            onClick = followState::resumeNow,
            modifier = Modifier.align(Alignment.BottomEnd).testTag("return-to-live"),
        ) {
            Icon(Icons.Default.KeyboardArrowDown, contentDescription = "Return to live")
        }
    }
}
