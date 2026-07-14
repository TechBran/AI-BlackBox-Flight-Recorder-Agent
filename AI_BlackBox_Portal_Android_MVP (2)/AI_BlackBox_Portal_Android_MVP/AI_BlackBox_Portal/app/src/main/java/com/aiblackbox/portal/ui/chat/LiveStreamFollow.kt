package com.aiblackbox.portal.ui.chat

internal const val FOLLOW_RESUME_DELAY_MS = 5_000L

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
