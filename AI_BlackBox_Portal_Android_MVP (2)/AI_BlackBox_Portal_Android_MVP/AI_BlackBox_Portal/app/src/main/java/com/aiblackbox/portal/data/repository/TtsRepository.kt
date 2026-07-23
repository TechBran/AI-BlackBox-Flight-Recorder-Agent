package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.CancellationException
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.booleanOrNull
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import kotlin.math.roundToInt

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

        // D10 (Task 7.9): on-box (Qwen) synthesis can wait on a GPU group swap.
        // Both the settings preview and the chat speak path show a "loading
        // models…" affordance when the first byte is slower than
        // SLOW_FIRST_BYTE_MS. Kept here so the two surfaces can never drift on
        // the provider sentinel or the threshold.
        const val ON_BOX_PROVIDER = "qwen"
        const val SLOW_FIRST_BYTE_MS = 1500L

        /**
         * Slow-first-byte watchdog body. Waits [SLOW_FIRST_BYTE_MS]; if the
         * request is still in flight when it fires, invokes [onSlow]. Callers
         * cancel the enclosing coroutine the instant the first byte arrives, so
         * a warm/fast synthesis never trips it. Extracted (vs inlined at each
         * call site) so the timing transition is unit-testable without an
         * Android ViewModel — see TtsSlowFirstByteTest.
         */
        suspend fun awaitSlowFirstByte(stillInFlight: () -> Boolean, onSlow: () -> Unit) {
            kotlinx.coroutines.delay(SLOW_FIRST_BYTE_MS)
            if (stillInFlight()) onSlow()
        }

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
        provider: String = "openai",
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
            append(",\"provider\":\"$provider\"")
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
                // Generic synchronous /tts/batch path. Pass the PARSED provider
                // through instead of hardcoding "openai", so on-box voices
                // (local:/qwen:) reach their real backend branch instead of
                // being mislabeled "openai" (which 400s on a non-openai id).
                generateTts(
                    text = text,
                    voice = config.voice,
                    model = config.model,
                    provider = config.provider,
                    operator = operator
                )
            }
        }
    }

    // =========================================================================
    // On-box TTS queue (B3, 2026-07-22) — Android client for the B1 async
    // server queue. On-box (qwen) voices submit ONE job (POST /tts/queue) and
    // poll GET /tts/task/{id} every TtsQueue.POLL_MS instead of holding a
    // multi-minute /tts/batch request open against OkHttp's read timeout.
    // Cloud providers NEVER come through here (their paths are untouched).
    // Fail-open: a 404 from POST /tts/queue (older backend) raises
    // TtsQueueUnavailableException so the caller falls back to /tts/batch.
    // All state parsing / status strings / backoff live in the PURE TtsQueue
    // object below (unit-tested in TtsQueueStateTest, like VoicePicker).
    // =========================================================================

    /**
     * Submit one on-box TTS job. Returns the queue task_id.
     * @throws TtsQueueUnavailableException when the backend has no /tts/queue
     *         (HTTP 404) — the caller should use the direct /tts/batch path.
     * @throws Exception on 400/503/network failures (surfaced to the user; the
     *         direct path would hit the same wall, so no fallback).
     */
    suspend fun submitQueue(
        text: String,
        voice: String,
        operator: String = "unknown",
    ): String {
        // Mirror the direct path's buildTtsBatchBody: strip non-speakable
        // content client-side (server sanitize_for_speech still runs too).
        val body = buildJsonObject {
            put("text", com.aiblackbox.portal.util.SpeakableText.stripNonSpeakable(text))
            put("provider", "qwen")
            put("voice", "qwen:$voice")
            put("operator", operator)
        }.toString()
        val raw = try {
            api.post("/tts/queue", body)
        } catch (e: ApiHttpException) {
            if (e.code == 404) {
                Log.i(TAG, "POST /tts/queue 404 (older backend) — direct /tts/batch fallback")
                throw TtsQueueUnavailableException()
            }
            throw e
        }
        val taskId = try {
            json.parseToJsonElement(raw).jsonObject["task_id"]?.jsonPrimitive?.contentOrNull
        } catch (e: Exception) {
            null
        }
        if (taskId.isNullOrBlank()) throw Exception("TTS queue submit returned no task_id")
        Log.d(TAG, "submitQueue: task_id=$taskId (${text.length} chars, voice=$voice)")
        return taskId
    }

    /** One fetch of GET /tts/task/{id}, classified for the poll state machine. */
    private suspend fun fetchTask(taskId: String): TtsQueueFetch = try {
        TtsQueueFetch.Body(api.get("/tts/task/$taskId"))
    } catch (e: CancellationException) {
        throw e
    } catch (e: ApiHttpException) {
        if (e.code == 404) TtsQueueFetch.NotFound else TtsQueueFetch.TransportError
    } catch (e: Exception) {
        Log.w(TAG, "pollTask transport error: ${e.message}")
        TtsQueueFetch.TransportError
    }

    /**
     * Poll a queued job ONCE and return its typed state. Null on a transient
     * transport/parse error (caller keeps polling); a 404 maps to the terminal
     * "queue task lost" failure (the in-memory B1 queue dropped on restart).
     */
    suspend fun pollTask(taskId: String): TtsQueueState? = when (val f = fetchTask(taskId)) {
        is TtsQueueFetch.Body -> TtsQueue.parseState(f.json)
        TtsQueueFetch.NotFound -> TtsQueue.lostTaskFailure()
        TtsQueueFetch.TransportError -> null
    }

    /**
     * Full submit -> poll(1.5s cadence, error backoff) -> download flow for an
     * on-box clip. Returns the finished WAV bytes.
     * [onStatus] fires on every non-terminal poll with the typed state (feed
     * TtsQueue.statusLine into the bubble chip).
     * @throws TtsQueueUnavailableException 404 on submit — use /tts/batch.
     * @throws TtsQueueFailedException terminal queue failure (carries
     *         [TtsQueueFailedException.retryable] + the task id).
     */
    suspend fun generateViaQueue(
        text: String,
        voice: String,
        operator: String = "unknown",
        onStatus: (TtsQueueState) -> Unit = {},
    ): ByteArray {
        val taskId = submitQueue(text, voice, operator)
        val terminal = TtsQueue.pollLoop(
            fetch = { fetchTask(taskId) },
            onStatus = onStatus,
        )
        when (terminal) {
            is TtsQueueState.Done -> {
                val bytes = api.getBytes(terminal.audioUrl)
                if (bytes == null || bytes.isEmpty()) {
                    throw TtsQueueFailedException(
                        taskId, "audio download failed (${terminal.audioUrl})", retryable = true
                    )
                }
                Log.d(TAG, "generateViaQueue: $taskId done — ${bytes.size} bytes")
                return bytes
            }
            is TtsQueueState.Failed ->
                throw TtsQueueFailedException(taskId, terminal.error, terminal.retryable)
            else ->  // pollLoop only returns terminal states
                throw TtsQueueFailedException(taskId, "unexpected non-terminal state", retryable = true)
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

// On-box audio (D2) is DELIBERATELY absent from this compiled-in fallback:
//   • qwen (on-box TTS) is dynamic-only — exactly like ElevenLabs/local, it comes
//     ONLY from the live GET /tts/catalog when the on-box stack is healthy. Baking
//     it in here would advertise non-functional voices on a stack-less box whose
//     catalog is unreachable, breaking the fail-open invariant. Guarded by
//     QwenVoiceRoutingTest.offlineFallback_hasNoQwenGroup and matching the web D1
//     static fallback (Portal/index.html), which is likewise cloud-only.
//   • whisper is STT-only (no TTS voices) and never belongs in a voice picker.
// The provider-first two-step picker (VoicePicker) derives its providers from
// whatever groups are present, so the live catalog surfaces qwen when available.
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

/**
 * Pure derivation for the provider-first TWO-STEP voice picker (D2). Given the
 * live/offline [VoiceGroup] list and the persisted `provider:voice` id, decide
 * which provider group the picker shows and which voices populate the second
 * dropdown. Compose/Android-free so it is unit-testable on the JVM
 * (VoicePickerDerivationTest). Mirrors the web helpers in
 * Portal/modules/tts-stt.js (providerOfVoiceId / resolveVoiceSelection).
 */
object VoicePicker {
    /** The group owning [voiceId]: exact voice match first, else the group whose
     *  voices share the `provider:` prefix of [voiceId]. Null if neither. */
    fun groupForVoice(groups: List<VoiceGroup>, voiceId: String): VoiceGroup? {
        groups.firstOrNull { g -> g.voices.any { it.id == voiceId } }?.let { return it }
        val prefix = voiceId.substringBefore(':', "")
        if (prefix.isEmpty()) return null
        return groups.firstOrNull { g ->
            g.voices.any { it.id.substringBefore(':', "") == prefix }
        }
    }

    /** Which provider group the two-step picker should display for the persisted
     *  [currentVoice]: its owning group if resolvable, else the first group.
     *  Null only for an empty catalog. */
    fun selectedGroup(groups: List<VoiceGroup>, currentVoice: String): VoiceGroup? =
        groupForVoice(groups, currentVoice) ?: groups.firstOrNull()

    /** The voices that populate the second dropdown for the chosen [group]. */
    fun voicesFor(group: VoiceGroup?): List<VoiceOption> = group?.voices ?: emptyList()
}

// =============================================================================
// On-box TTS queue — PURE core (B3, 2026-07-22)
//
// Typed states, status strings, backoff math, and the poll state machine for
// the B1 async server queue (POST /tts/queue -> GET /tts/task/{id}). Kept
// Android/OkHttp-free so it is unit-testable on the JVM (TtsQueueStateTest),
// exactly like VoicePicker above. Mirrors the web helpers in
// Portal/modules/tts-stt.js (ttsQueueIndicatorText / ttsQueuePollDelayMs /
// pollTtsQueueTask) so the two surfaces cannot drift on semantics.
// =============================================================================

/** Typed state of one queued on-box TTS job (from GET /tts/task/{id}). */
sealed class TtsQueueState {
    /** Waiting in the FIFO; [ahead] jobs run before this one. */
    data class Queued(val ahead: Int) : TtsQueueState()

    /** On the GPU: sub-batch [subbatch] of [total], [elapsedS] in, ~[etaS] left. */
    data class Generating(
        val subbatch: Int,
        val total: Int,
        val elapsedS: Double,
        val etaS: Double,
    ) : TtsQueueState()

    /** Finished — WAV served at [audioUrl] (resolve against baseUrl). */
    data class Done(val audioUrl: String) : TtsQueueState()

    /** Terminal failure ([retryable] = worth resubmitting). The server's
     *  `cancelled` state folds in here as a non-retryable failure. */
    data class Failed(val error: String, val retryable: Boolean) : TtsQueueState()
}

/** One classified poll fetch — production wraps HTTP, tests inject fakes. */
sealed class TtsQueueFetch {
    /** 200 with a body (parse it — garbage counts as a transient error). */
    data class Body(val json: String) : TtsQueueFetch()

    /** 404: the in-memory B1 queue dropped this task (service restart). */
    object NotFound : TtsQueueFetch()

    /** Network/timeout/5xx — transient, poll again with backoff. */
    object TransportError : TtsQueueFetch()
}

/** POST /tts/queue returned 404: backend predates B1 — use direct /tts/batch. */
class TtsQueueUnavailableException :
    Exception("Backend has no /tts/queue (older build) — falling back to /tts/batch")

/** A queue job ended in a terminal failure. */
class TtsQueueFailedException(
    val taskId: String,
    message: String,
    val retryable: Boolean,
) : Exception(message)

object TtsQueue {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** Base poll cadence for GET /tts/task/{id}. */
    const val POLL_MS = 1500L

    /** Cap for the error-backoff poll delay. */
    const val POLL_MAX_MS = 12000L

    /** Consecutive fetch errors before the poller gives up (retryable). */
    const val MAX_CONSECUTIVE_ERRORS = 8

    /** Terminal failure for a task the restarted service no longer knows. */
    fun lostTaskFailure() =
        TtsQueueState.Failed("queue task lost (service restarted)", retryable = false)

    /**
     * Parse a raw GET /tts/task/{id} body into a typed state.
     * Null when unparseable or an unknown status — callers treat that as a
     * transient error, NOT a crash (a proxy error page must never throw).
     */
    fun parseState(raw: String): TtsQueueState? = try {
        val obj = json.parseToJsonElement(raw).jsonObject
        when (obj["status"]?.jsonPrimitive?.contentOrNull) {
            "queued" -> {
                val pos = obj["queue_position"]?.jsonPrimitive?.intOrNull ?: 1
                TtsQueueState.Queued(ahead = maxOf(0, pos - 1))
            }
            "generating" -> TtsQueueState.Generating(
                subbatch = obj["subbatch"]?.jsonPrimitive?.intOrNull ?: 0,
                total = obj["subbatches_total"]?.jsonPrimitive?.intOrNull ?: 0,
                elapsedS = obj["elapsed_s"]?.jsonPrimitive?.doubleOrNull ?: 0.0,
                etaS = obj["eta_s"]?.jsonPrimitive?.doubleOrNull ?: 0.0,
            )
            "done" -> {
                val url = obj["audio_url"]?.jsonPrimitive?.contentOrNull
                if (url.isNullOrBlank()) {
                    TtsQueueState.Failed("done without audio_url", retryable = false)
                } else {
                    TtsQueueState.Done(url)
                }
            }
            "failed" -> TtsQueueState.Failed(
                error = obj["error"]?.jsonPrimitive?.contentOrNull
                    ?.takeIf { it.isNotBlank() } ?: "Audio generation failed",
                retryable = obj["retryable"]?.jsonPrimitive?.booleanOrNull ?: false,
            )
            "cancelled" -> TtsQueueState.Failed("Audio cancelled", retryable = false)
            else -> null
        }
    } catch (e: Exception) {
        null
    }

    /** True when polling must stop. */
    fun isTerminal(state: TtsQueueState): Boolean =
        state is TtsQueueState.Done || state is TtsQueueState.Failed

    /**
     * Poll delay with exponential backoff on consecutive FETCH ERRORS only —
     * a healthy loop stays at [POLL_MS] (1500, 3000, 6000, 12000 cap).
     */
    fun pollDelayMs(consecutiveErrors: Int): Long {
        val n = maxOf(0, consecutiveErrors)
        var delay = POLL_MS
        repeat(minOf(n, 8)) { delay = minOf(delay * 2, POLL_MAX_MS) }
        return delay
    }

    /** Format seconds as m:ss for the progress chip (e.g. "1:15"). */
    fun formatClock(seconds: Double): String {
        val s = maxOf(0, seconds.toInt())
        return "${s / 60}:${(s % 60).toString().padStart(2, '0')}"
    }

    /**
     * Bubble-chip text for a typed state. Pure: state in -> string out.
     *   Queued(0)  -> "Queued — starting next"
     *   Queued(2)  -> "Queued — 2 ahead"
     *   Generating -> "Generating 2/5… 1:15, ~30s left"
     *   Done       -> "Audio ready"
     *   Failed     -> "Audio failed: <error>"
     */
    fun statusLine(state: TtsQueueState): String = when (state) {
        is TtsQueueState.Queued ->
            if (state.ahead <= 0) "Queued — starting next"
            else "Queued — ${state.ahead} ahead"
        is TtsQueueState.Generating -> {
            val clock = formatClock(state.elapsedS)
            val seg = if (state.total > 0) {
                " ${state.subbatch.coerceIn(1, state.total)}/${state.total}"
            } else ""
            val eta = state.etaS.roundToInt()
            val etaTxt = if (eta > 0) ", ~${eta}s left" else ""
            "Generating$seg… $clock$etaTxt"
        }
        is TtsQueueState.Done -> "Audio ready"
        is TtsQueueState.Failed -> "Audio failed: ${state.error}"
    }

    /**
     * The poll state machine: fetch -> classify -> repeat until terminal.
     * Only ever returns [TtsQueueState.Done] or [TtsQueueState.Failed]:
     *  - [TtsQueueFetch.NotFound] -> [lostTaskFailure] (restart dropped the
     *    in-memory queue; terminal, retryable only via resubmit)
     *  - [MAX_CONSECUTIVE_ERRORS] transport/parse errors in a row -> a
     *    retryable "lost contact" failure
     *  - a healthy body resets the error counter; non-terminal states go to
     *    [onStatus] and the loop sleeps [pollDelayMs] via [delayMs]
     * [fetch] and [delayMs] are injected so tests drive it with fake
     * responses and recorded delays (no real clock, no HTTP).
     */
    suspend fun pollLoop(
        fetch: suspend () -> TtsQueueFetch,
        onStatus: (TtsQueueState) -> Unit = {},
        delayMs: suspend (Long) -> Unit = { kotlinx.coroutines.delay(it) },
    ): TtsQueueState {
        var errors = 0
        while (true) {
            var state: TtsQueueState? = null
            when (val f = fetch()) {
                is TtsQueueFetch.Body -> {
                    state = parseState(f.json)
                    errors = if (state == null) errors + 1 else 0
                }
                TtsQueueFetch.NotFound -> return lostTaskFailure()
                TtsQueueFetch.TransportError -> errors += 1
            }
            if (errors >= MAX_CONSECUTIVE_ERRORS) {
                return TtsQueueState.Failed(
                    "lost contact with the TTS queue", retryable = true
                )
            }
            if (state != null) {
                if (isTerminal(state)) return state
                onStatus(state)
            }
            delayMs(pollDelayMs(errors))
        }
    }
}
