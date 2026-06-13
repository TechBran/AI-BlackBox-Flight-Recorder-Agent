package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.jsonArray
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
                    "elevenlabs" -> ""  // model chosen server-side (eleven_v3 default)
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
    // ElevenLabs TTS — POST /tts with return_json (synchronous, returns audio_url).
    // The backend routes any voice prefixed "elevenlabs:" to ElevenLabs synthesis
    // (quality-first eleven_v3). Without this, elevenlabs voices fell into the
    // Gemini/Cloud branch and the raw voice_id was rejected as a Google voice name.
    // =========================================================================
    suspend fun generateElevenLabsTts(
        text: String,
        voiceId: String,
        operator: String = "Brandon"
    ): TtsResponse {
        val sanitized = text
            .take(TTS_MAX_CHARS)
            .replace("\\", "\\\\")
            .replace("\"", "\\\"")
            .replace("\n", "\\n")

        // voiceId is the raw id (prefix stripped by parseVoice); re-prefix so the
        // backend detects the ElevenLabs provider.
        val body = buildString {
            append("{\"text\":\"$sanitized\"")
            append(",\"voice\":\"elevenlabs:$voiceId\"")
            append(",\"provider\":\"elevenlabs\"")
            append(",\"return_json\":true")
            append(",\"operator\":\"$operator\"")
            append("}")
        }

        val response = api.post("/tts", body)
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
    // Gemini task poll — wait for a completed task's audio url.
    // Parsing is based on GeminiProTtsScreen's task-status handling
    // (status / result_url / error_message); bounded with a timeout.
    // ~90s ceiling: Gemini Pro TTS can exceed 60s under load.
    // =========================================================================
    suspend fun pollGeminiTaskForUrl(taskId: String, attempts: Int = 90, intervalMs: Long = 1000): String {
        repeat(attempts) {
            val raw = api.get("/tasks/status/$taskId")
            val o = json.parseToJsonElement(raw).jsonObject
            val status = o["status"]?.jsonPrimitive?.content ?: "pending"
            when {
                status.equals("completed", ignoreCase = true) -> {
                    val url = o["result_url"]?.jsonPrimitive?.content
                    if (!url.isNullOrBlank()) return url
                }
                status.equals("failed", ignoreCase = true) -> {
                    val err = o["error_message"]?.jsonPrimitive?.content ?: "Generation failed"
                    throw Exception(err)
                }
            }
            kotlinx.coroutines.delay(intervalMs)
        }
        throw Exception("Gemini TTS preview timed out")
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
            "elevenlabs" -> {
                // ElevenLabs TTS — synchronous, returns audio_url directly
                generateElevenLabsTts(text = text, voiceId = config.voice, operator = operator)
            }
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
    // Voice Catalog — GET /tts/catalog (live catalog with offline fallback)
    // Backend shape: {"groups":[{"id","label","voices":[{"id","name","description"}]}]}
    // Returns TTS_VOICE_GROUPS on ANY failure (network, parse, empty).
    // =========================================================================
    suspend fun fetchCatalog(): List<VoiceGroup> = try {
        val raw = api.get("/tts/catalog")
        val groups = json.parseToJsonElement(raw).jsonObject["groups"]?.jsonArray
            ?: return TTS_VOICE_GROUPS
        val parsed = groups.map { g ->
            val o = g.jsonObject
            VoiceGroup(
                label = o["label"]?.jsonPrimitive?.content ?: "",
                voices = (o["voices"]?.jsonArray ?: JsonArray(emptyList())).map { v ->
                    val vo = v.jsonObject
                    VoiceOption(
                        id = vo["id"]!!.jsonPrimitive.content,
                        name = vo["name"]!!.jsonPrimitive.content,
                        description = vo["description"]?.jsonPrimitive?.content ?: "",
                    )
                },
            )
        }
        if (parsed.isEmpty()) TTS_VOICE_GROUPS else parsed
    } catch (e: Exception) {
        Log.w(TAG, "fetchCatalog failed, using offline fallback: ${e.message}")
        TTS_VOICE_GROUPS
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
 * Gemini Flash and Gemini Pro fallback groups AND the Gemini Pro TTS
 * generation screen (DRY — defined once, the single source of truth).
 */
val GEMINI_TTS_VOICE_PAIRS: List<Pair<String, String>> = listOf(
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
        GEMINI_TTS_VOICE_PAIRS.map { (n, d) -> VoiceOption("gemini-flash:$n", n, d) }
    ),
    VoiceGroup("Gemini Pro TTS",
        GEMINI_TTS_VOICE_PAIRS.map { (n, d) -> VoiceOption("gemini-pro:$n", n, d) }
    ),
)
