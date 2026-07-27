package com.aiblackbox.portal.data.voice

/**
 * The live voice session's two sample rates, named once.
 *
 * They used to be bare literals inside VoiceViewModel.startMic() and initAudioPlayback(). That was
 * harmless while the only consumer was the AudioRecord/AudioTrack they sat next to, but the Aurora
 * ribbon puts a SECOND consumer on each of them (an AuroraAnalyser probes fixed frequencies and has
 * to be told the stream's real rate). Two literals in two methods is exactly the shape that drifts:
 * a 16 kHz mic analysed as though it were 24 kHz reads every band 1.5x too high and quietly loses
 * the top one to its Nyquist guard. Pure + unit-tested, like [shouldHoldMic] beside it.
 */
object VoiceSampleRates {

    /**
     * Mic capture rate. OpenAI's realtime transcription rejects anything below 24 kHz; Gemini and
     * Grok take 16 kHz, which is cheaper on the wire and still clears every analyser probe.
     */
    fun mic(backend: VoiceBackend): Int = when (backend) {
        VoiceBackend.GPT_REALTIME -> 24000
        else -> 16000
    }

    /** Every backend streams its audio back at 24 kHz; this is the AudioTrack's rate. */
    const val AI_OUTPUT = 24000
}
