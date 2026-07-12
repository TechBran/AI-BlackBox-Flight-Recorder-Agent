package com.aiblackbox.portal.data.voice

/**
 * Optional per-connection configuration for VoiceClient.
 *
 * Each field is independent and Optional — when null, backend uses its default.
 * Fields are wire-encoded into the WebSocket URL query string by VoiceClient.kt.
 *
 * - OpenAI Realtime: model, vadType (server_vad|semantic_vad), vadEagerness
 *   (low|medium|high|auto for semantic_vad), idleTimeoutMs (server_vad only).
 * - Gemini Live: model, vadStart/vadEnd (UPPERCASE LOW|MEDIUM|HIGH),
 *   thinkingLevel (lowercase minimal|low|medium|high, 3.1 model only).
 * - P6a translate mode: mode ("translate"|null) + targetLanguage (BCP-47);
 *   OpenAI + Gemini only (Grok has no translate model).
 *
 * Per docs/plans/2026-05-19-live-models-upgrade.md T11 + audit M2.
 */
data class VoiceSessionConfig(
    val model: String? = null,
    val vadType: String? = null,
    val vadEagerness: String? = null,
    val idleTimeoutMs: Int? = null,
    val vadStart: String? = null,
    val vadEnd: String? = null,
    val thinkingLevel: String? = null,
    /** Gemini affective dialog / proactive audio — 2.5 native-audio only; null = omit. */
    val affective: Boolean? = null,
    val proactive: Boolean? = null,
    /** P3.12: voice-agent preset id → ?agent= on the WS URL (workstream 3). */
    val agentId: String? = null,
    /** P3.19: Grok Live reasoning.effort (high|none, grok-voice-think-fast-1.0). */
    val reasoningEffort: String? = null,
    val mode: String? = null,            // P6a: "translate" | null (normal session)
    val targetLanguage: String? = null,  // P6a: BCP-47 target when mode == "translate"
)
