package com.aiblackbox.portal.data.voice

/**
 * P3.15: client-side mic gating during AI speech.
 * Grok: echo-prone — hold the mic while the AI speaks + POST_SPEECH_DELAY_MS after.
 * OpenAI/Gemini: leave the mic OPEN so server VAD hears barge-ins — the AEC stack
 * (VOICE_COMMUNICATION source + AcousticEchoCanceler + MODE_IN_COMMUNICATION)
 * suppresses speaker echo. Pure function — unit-tested.
 */
fun shouldHoldMic(
    backend: VoiceBackend,
    isAiSpeaking: Boolean,
    msSinceAiStopped: Long,
    postSpeechDelayMs: Long = VoiceClient.POST_SPEECH_DELAY_MS,
): Boolean {
    if (backend != VoiceBackend.GROK_LIVE) return false
    return isAiSpeaking || msSinceAiStopped < postSpeechDelayMs
}
