package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

// =============================================================================
// TtsRepository — aligned with Portal tts-stt.js
//
// Portal TTS flow:
//   1. Parse voice as "provider:voice" (e.g., "openai:alloy", "gemini-pro:Charon")
//   2. OpenAI path: POST /tts/batch → returns audio blob directly
//   3. Gemini path: POST /generate/gemini_tts → returns task_id → poll
//   4. Cache key: simpleHash(text + ':' + provider + ':' + voice)
//
// Portal STT flow:
//   1. Record via MediaRecorder (webm) or native Android mic
//   2. POST /stt as multipart → Whisper transcription → text
// =============================================================================

private const val TAG = "TtsRepo"

@Serializable
data class TtsResponse(
    val status: String = "",
    val audio_url: String = "",
    val voice: String = "",
    val model: String = "",
    val format: String = "",
    val size_bytes: Long = 0
)

@Serializable
data class GeminiTtsResponse(
    val task_id: String = "",
    val status: String = ""
)

/**
 * Parsed voice preference in "provider:voice" format.
 * Matches Portal's parseVoiceValue() function.
 */
data class VoiceConfig(
    val provider: String,  // "openai" or "gemini-pro"
    val voice: String,     // e.g., "alloy", "Charon"
    val model: String      // e.g., "tts-1-hd", "gemini-2.5-flash-tts"
)

class TtsRepository(private val api: BlackBoxApi) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    companion object {
        /** Maximum characters per TTS request (matches Portal TTS_MAX_CHARS) */
        const val TTS_MAX_CHARS = 4000

        /**
         * Parse "provider:voice" format into VoiceConfig.
         * Matches Portal parseVoiceValue().
         *
         * Examples:
         *   "openai:alloy" → VoiceConfig("openai", "alloy", "tts-1-hd")
         *   "gemini-flash:Zephyr" → VoiceConfig("gemini-flash", "Zephyr", "gemini-2.5-flash-tts")
         *   "gemini-pro:Charon" → VoiceConfig("gemini-pro", "Charon", "gemini-2.5-pro-tts")
         *   "onyx" → VoiceConfig("openai", "onyx", "tts-1-hd") (legacy fallback)
         */
        fun parseVoice(voiceValue: String): VoiceConfig {
            val parts = voiceValue.split(":", limit = 2)
            return if (parts.size == 2) {
                val provider = parts[0]
                val voice = parts[1]
                val model = when (provider) {
                    "gemini-flash" -> "gemini-2.5-flash-tts"
                    "gemini-pro" -> "gemini-2.5-pro-tts"
                    else -> "tts-1-hd"
                }
                VoiceConfig(provider, voice, model)
            } else {
                // Legacy: just a voice name, assume OpenAI
                VoiceConfig("openai", voiceValue, "tts-1-hd")
            }
        }
    }

    // =========================================================================
    // OpenAI TTS — POST /tts/batch (fast, returns audio URL directly)
    // Matches Portal generateTTSAudioWithVoice() OpenAI path
    // =========================================================================
    suspend fun generateTts(
        text: String,
        voice: String = "onyx",
        model: String = "tts-1-hd",
        format: String = "mp3",
        operator: String = "Brandon"
    ): TtsResponse {
        val sanitized = text
            .take(TTS_MAX_CHARS)
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")

        val body = buildString {
            append("{\"text\":\"$sanitized\"")
            append(",\"voice\":\"$voice\"")
            append(",\"model\":\"$model\"")
            append(",\"format\":\"$format\"")
            append(",\"provider\":\"openai\"")
            append(",\"operator\":\"$operator\"")
            append("}")
        }

        val response = api.post("/tts/batch", body)
        return json.decodeFromString(TtsResponse.serializer(), response)
    }

    // =========================================================================
    // Gemini Pro TTS — POST /generate/gemini_tts (async, returns task_id)
    // Matches Portal generateGeminiTTSChunk()
    // =========================================================================
    suspend fun generateGeminiTts(
        text: String,
        voice: String = "Charon",
        model: String = "gemini-2.5-flash-tts",
        operator: String = "Brandon"
    ): GeminiTtsResponse {
        val sanitized = text
            .take(TTS_MAX_CHARS)
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")

        val body = buildString {
            append("{\"text\":\"$sanitized\"")
            append(",\"voice_name\":\"$voice\"")
            append(",\"model\":\"$model\"")
            append(",\"operator\":\"$operator\"")
            append("}")
        }

        val response = api.post("/generate/gemini_tts", body)
        return json.decodeFromString(GeminiTtsResponse.serializer(), response)
    }

    // =========================================================================
    // Provider-aware TTS — routes to correct backend based on voice config
    // Matches Portal generateTTSAudioWithVoice()
    // =========================================================================
    suspend fun generateWithVoice(
        text: String,
        voiceValue: String,
        operator: String = "Brandon"
    ): TtsResponse {
        val config = parseVoice(voiceValue)
        Log.d(TAG, "generateWithVoice: provider=${config.provider}, voice=${config.voice}")

        return when (config.provider) {
            "gemini-pro", "gemini-flash" -> {
                // Gemini TTS is async — start task (polling handled by caller)
                val result = generateGeminiTts(
                    text = text,
                    voice = config.voice,
                    model = config.model,
                    operator = operator
                )
                TtsResponse(
                    status = "pending",
                    audio_url = "",
                    voice = config.voice,
                    model = config.model
                )
            }
            else -> {
                // OpenAI TTS — synchronous
                generateTts(
                    text = text,
                    voice = config.voice,
                    model = config.model,
                    operator = operator
                )
            }
        }
    }

    // =========================================================================
    // STT — Whisper transcription
    // =========================================================================
    suspend fun transcribe(audioBase64: String, sampleRate: Int = 16000): String {
        val body = """{"audio":"$audioBase64","sample_rate":$sampleRate,"format":"pcm16"}"""
        val response = api.post("/stt/json", body)
        val obj = json.parseToJsonElement(response)
        return obj.jsonObject["text"]?.jsonPrimitive?.content ?: ""
    }
}

// =============================================================================
// Voice List — matches Portal ttsVoiceSelect optgroups
// =============================================================================

data class VoiceOption(
    val id: String,         // "openai:alloy" or "gemini-pro:Charon"
    val name: String,       // "Alloy"
    val description: String // "Neutral, balanced"
)

data class VoiceGroup(
    val label: String,
    val voices: List<VoiceOption>
)

/**
 * Canonical Gemini TTS voice catalog (name → description).
 * Matches backend GEMINI_TTS_VOICE_DESCRIPTIONS. Shared by both the
 * Gemini Flash and Gemini Pro fallback groups (DRY — defined once).
 */
private val GEMINI_TTS_VOICES: List<Pair<String, String>> = listOf(
    "Zephyr" to "Bright, cheerful",
    "Puck" to "Playful, mischievous",
    "Charon" to "Calm, informative",
    "Kore" to "Clear, versatile",
    "Fenrir" to "Bold, confident",
    "Leda" to "Warm, youthful",
    "Orus" to "Deep, firm",
    "Aoede" to "Breezy, conversational",
    "Callirrhoe" to "Smooth, flowing",
    "Autonoe" to "Gentle, measured",
    "Enceladus" to "Rich, resonant",
    "Iapetus" to "Deep, steady",
    "Umbriel" to "Soft, mysterious",
    "Algieba" to "Warm, articulate",
    "Despina" to "Light, energetic",
    "Erinome" to "Serene, melodic",
    "Algenib" to "Crisp, precise",
    "Rasalgethi" to "Grand, theatrical",
    "Laomedeia" to "Graceful, elegant",
    "Achernar" to "Bright, radiant",
    "Alnilam" to "Strong, commanding",
    "Schedar" to "Regal, distinguished",
    "Gacrux" to "Earthy, grounded",
    "Pulcherrima" to "Beautiful, refined",
    "Achird" to "Friendly, approachable",
    "Zubenelgenubi" to "Balanced, neutral",
    "Vindemiatrix" to "Mature, wise",
    "Sadachbia" to "Lucky, optimistic",
    "Sadaltager" to "Hopeful, bright",
    "Sulafat" to "Lyrical, musical",
)

val TTS_VOICE_GROUPS = listOf(
    VoiceGroup("OpenAI TTS HD", listOf(
        VoiceOption("openai:alloy", "Alloy", "Neutral, balanced"),
        VoiceOption("openai:ash", "Ash", "Clear, direct"),
        VoiceOption("openai:ballad", "Ballad", "Warm, gentle"),
        VoiceOption("openai:coral", "Coral", "Friendly, conversational"),
        VoiceOption("openai:echo", "Echo", "Smooth, authoritative"),
        VoiceOption("openai:fable", "Fable", "Expressive, British"),
        VoiceOption("openai:nova", "Nova", "Energetic, confident"),
        VoiceOption("openai:onyx", "Onyx", "Deep, authoritative"),
        VoiceOption("openai:sage", "Sage", "Thoughtful, measured"),
        VoiceOption("openai:shimmer", "Shimmer", "Soft, ethereal"),
        VoiceOption("openai:verse", "Verse", "Poetic, dramatic"),
    )),
    VoiceGroup("Gemini Flash TTS",
        GEMINI_TTS_VOICES.map { (n, d) -> VoiceOption("gemini-flash:$n", n, d) }
    ),
    VoiceGroup("Gemini Pro TTS",
        GEMINI_TTS_VOICES.map { (n, d) -> VoiceOption("gemini-pro:$n", n, d) }
    ),
)
