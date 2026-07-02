package com.aiblackbox.portal.overlay

import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import kotlinx.coroutines.flow.asStateFlow
import kotlinx.coroutines.flow.update

/**
 * Singleton bridge between OverlayService (state producer) and XrOverlayActivity (state consumer).
 * On phone devices this is never used. On XR devices, the Service writes state here and the
 * Compose-based Activity observes it via StateFlow.
 */
object OverlayBridge {

    // ---- State ----

    data class TranscriptEntry(
        val speaker: String,
        val text: String,
        val timestamp: Long = System.currentTimeMillis()
    )

    data class OverlayState(
        // Connection
        val isConnected: Boolean = false,
        val isRecording: Boolean = false,
        val isAISpeaking: Boolean = false,
        val isScreenSharing: Boolean = false,
        val isExpanded: Boolean = false,
        val isUserMuted: Boolean = false,

        // Status
        val statusText: String = "Disconnected",

        // Selectors
        val currentModel: String = "Gemini Live",
        val currentVoice: String = "Charon",
        val currentOperator: String = "Brandon",

        // Lists for dropdowns
        val models: List<String> = listOf("Gemini Live", "GPT Realtime", "Grok Live"),
        val voices: List<String> = listOf("Charon", "Puck", "Kore", "Aoede", "Fenrir", "Orus"),
        val operators: List<String> = listOf("Brandon", "default"),

        // Transcript
        val transcriptEntries: List<TranscriptEntry> = emptyList(),
        val liveResponseText: String = "",

        // Progress
        val isConnecting: Boolean = false,

        // (M6.3) True while an AI REMOTE-CONTROL session is actuating THIS device (driven by
        // RemoteSessionBus). The XR panel renders the "AI is controlling this device" consent
        // banner + STOP kill switch when true. Additive; defaults false (phone/idle path
        // unchanged). This is the in-headset equivalent of OverlayService's phone banner /
        // NotificationListenerFgs's fail-safe STOP notification — the XR compositor doesn't
        // surface the TYPE_APPLICATION_OVERLAY banner, so the consent lives in the panel.
        val controlSessionActive: Boolean = false,

        // Lifecycle — set true when service is stopping so XR Activity can finish()
        val shouldFinish: Boolean = false
    )

    private val _state = MutableStateFlow(OverlayState())
    val state: StateFlow<OverlayState> = _state.asStateFlow()

    /**
     * (M2) Atomically update state. `MutableStateFlow.update` applies [transform] in a
     * compare-and-set loop, so a state transition (e.g. `controlSessionActive`) can't be lost to a
     * concurrent read-modify-write — the previous `_state.value = transform(_state.value)` was a
     * non-atomic read-then-write that could drop an off-main writer's update. Usually called on the
     * main thread, but now safe regardless of caller thread.
     */
    fun updateState(transform: (OverlayState) -> OverlayState) {
        _state.update(transform)
    }

    /** Reset to defaults when service is destroyed. */
    fun reset() {
        _state.value = OverlayState()
        commandListener = null
    }

    // ---- Commands (XR Activity → Service) ----

    interface CommandListener {
        fun toggleConnect()
        fun toggleMic()
        fun toggleScreenShare()
        fun toggleExpanded()
        fun selectModel(model: String)
        fun selectVoice(voice: String)
        fun selectOperator(operator: String)
        fun openCamera()
        fun openPortal()
        fun minimize()

        // (M6.3) The XR consent-banner kill switch: STOP the active AI remote-control session.
        // The Service implementation routes it to RemoteSessionBus.stop() (fail-safe: clears the
        // session + records the kill so subsequent /action frames are refused).
        fun stopRemoteControl()
    }

    private var commandListener: CommandListener? = null

    fun registerCommandListener(listener: CommandListener) {
        commandListener = listener
    }

    fun unregisterCommandListener() {
        commandListener = null
    }

    // Command dispatchers — called from Compose UI
    fun toggleConnect() = commandListener?.toggleConnect()
    fun toggleMic() = commandListener?.toggleMic()
    fun toggleScreenShare() = commandListener?.toggleScreenShare()
    fun toggleExpanded() = commandListener?.toggleExpanded()
    fun selectModel(model: String) = commandListener?.selectModel(model)
    fun selectVoice(voice: String) = commandListener?.selectVoice(voice)
    fun selectOperator(operator: String) = commandListener?.selectOperator(operator)
    fun openCamera() = commandListener?.openCamera()
    fun openPortal() = commandListener?.openPortal()
    fun minimize() = commandListener?.minimize()
    fun stopRemoteControl() = commandListener?.stopRemoteControl()
}
