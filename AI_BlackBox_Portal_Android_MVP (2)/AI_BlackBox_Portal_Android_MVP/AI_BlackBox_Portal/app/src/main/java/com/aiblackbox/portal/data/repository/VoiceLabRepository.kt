package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import java.io.File
import java.io.IOException
import java.net.URLEncoder

// =============================================================================
// VoiceLabRepository — ElevenLabs Voice Lab (Task 25)
//
// Wraps the DONE backend routes:
//   POST   /elevenlabs/voices/clone        (multipart) → {voice_id, requires_verification}
//   POST   /elevenlabs/voices/design       (json)      → {text, previews:[...]}
//   POST   /elevenlabs/voices/design/save  (json)      → {voice_id}
//   GET    /elevenlabs/voices                          → {my_voices:[...], premade:[...]}
//   DELETE /elevenlabs/voices/{voice_id}               → {ok, in_use:[...]}
//   GET    /elevenlabs/status                          → {configured, tier, features:{...}}
//
// The clone path needs a multipart body with multiple file parts plus text
// form fields, which BlackBoxApi.uploadFile (single file) can't express, so it
// builds a MultipartBody.Builder() directly — mirroring AudioRecorderManager
// .transcribe()'s manual multipart + the shared OkHttpClient (api.getClient()).
// Errors are surfaced as VoiceLabException carrying the HTTP status so the UI
// can special-case 422 (missing consent) / 400.
// =============================================================================

private const val TAG = "VoiceLabRepo"

class VoiceLabException(val status: Int, message: String) : Exception(message)

/** GET /elevenlabs/status → gating for the whole screen. */
data class ElevenLabsStatus(
    val configured: Boolean = false,
    val tier: String = "",
    val instantVoiceCloning: Boolean = false,
)

/** A voice row (my_voices / premade). */
data class ElevenVoice(
    val id: String,
    val name: String,
    val description: String = "",
    val previewUrl: String = "",
    val category: String = "",
)

data class VoiceLists(
    val myVoices: List<ElevenVoice> = emptyList(),
    val premade: List<ElevenVoice> = emptyList(),
)

/** One design preview candidate (POST /elevenlabs/voices/design). */
data class DesignPreview(
    val generatedVoiceId: String,
    val audioUrl: String,
    val durationSecs: Double = 0.0,
    val language: String = "",
)

data class DesignResult(
    val text: String = "",
    val previews: List<DesignPreview> = emptyList(),
)

/** Result of cloning a voice (POST /elevenlabs/voices/clone). */
data class CloneResult(
    val voiceId: String,
    val requiresVerification: Boolean = false,
)

/** Result of DELETE — `inUse` is the list of consumers blocking/affected by deletion. */
data class DeleteResult(
    val ok: Boolean,
    val inUse: List<String> = emptyList(),
)

/**
 * One community-library voice (GET /elevenlabs/library). `publicOwnerId` + `voiceId`
 * are the coordinates the add endpoint needs; accent/gender/age build the sub-line.
 * All strings are non-null (defaulted to "") so the UI never NPEs on sparse rows.
 */
data class SharedVoice(
    val publicOwnerId: String,
    val voiceId: String,
    val name: String,
    val previewUrl: String = "",
    val accent: String = "",
    val gender: String = "",
    val age: String = "",
    val description: String = "",
)

/** One xAI custom (cloned) Grok voice. Tolerates voice_id|id key naming. */
data class XaiVoice(val voiceId: String, val name: String)

// =============================================================================
// On-Box (Qwen3-TTS) — wire contracts for the /qwen/voices/* + /local-models/
// status routes. Parse functions are top-level internal (like
// parseXaiVoicesResponse) so the offline unit tests exercise them directly.
// =============================================================================

/**
 * GET /local-models/status → gate for the On-Box zone. `available` requires the
 * stack to be healthy AND the tts capability enabled; `ttsEnabled` fails OPEN
 * (true) when the routing block is absent so older/leaner status shapes that
 * only report `healthy` still light the zone (Portal parity).
 */
data class QwenTtsStatus(
    val healthy: Boolean = false,
    val ttsEnabled: Boolean = true,
) {
    val available: Boolean get() = healthy && ttsEnabled
}

/** One saved on-box clone/design profile (GET /qwen/voices). */
data class QwenVoice(
    val slug: String,
    val name: String,
    val variant: String = "",
)

/**
 * One design preview candidate (POST /qwen/voices/design). The member returns
 * base64 WAV (`audio_b64`) because it is loopback-only; `audioUrl` is kept as a
 * defensive fallback should the contract ever emit a URL (Portal parity).
 */
data class QwenDesignPreview(
    val generatedVoiceId: String,
    val audioB64: String = "",
    val sampleRate: Int = 0,
    val audioUrl: String = "",
)

/** Parse GET /local-models/status — minimal fields only (healthy + routing.tts). */
internal fun parseQwenStatusResponse(raw: String): QwenTtsStatus {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    val healthy = o["healthy"]?.jsonPrimitive?.contentOrNull?.toBoolean() ?: false
    val tts = o["routing"]?.jsonObject?.get("tts")?.jsonObject
    val ttsEnabled = tts?.get("enabled")?.jsonPrimitive?.contentOrNull?.toBoolean()
        ?: true // routing absent → fail-open on `healthy` alone
    return QwenTtsStatus(healthy = healthy, ttsEnabled = ttsEnabled)
}

/** Parse GET /qwen/voices → {voices:[{slug, name, variant}]}. Slugless rows skipped. */
internal fun parseQwenVoicesResponse(raw: String): List<QwenVoice> {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    return (o["voices"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
        val vo = el.jsonObject
        val slug = vo["slug"]?.jsonPrimitive?.contentOrNull ?: return@mapNotNull null
        QwenVoice(
            slug = slug,
            name = vo["name"]?.jsonPrimitive?.contentOrNull ?: slug,
            variant = vo["variant"]?.jsonPrimitive?.contentOrNull ?: "",
        )
    }
}

/** Parse POST /qwen/voices/design → previews[{generated_voice_id, audio_b64, sample_rate}]. */
internal fun parseQwenDesignResponse(raw: String): List<QwenDesignPreview> {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    return (o["previews"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
        val po = el.jsonObject
        val gid = po["generated_voice_id"]?.jsonPrimitive?.contentOrNull
            ?: return@mapNotNull null
        QwenDesignPreview(
            generatedVoiceId = gid,
            audioB64 = po["audio_b64"]?.jsonPrimitive?.contentOrNull ?: "",
            sampleRate = po["sample_rate"]?.jsonPrimitive?.contentOrNull?.toIntOrNull() ?: 0,
            audioUrl = po["audio_url"]?.jsonPrimitive?.contentOrNull ?: "",
        )
    }
}

/** Parse a clone / design-save response → voice_id ("" when absent). */
internal fun parseQwenVoiceIdResponse(raw: String): String {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    return j.parseToJsonElement(raw).jsonObject["voice_id"]
        ?.jsonPrimitive?.contentOrNull ?: ""
}

/**
 * Result of an on-box clone (POST /qwen/voices/clone). Servers >= ba81b8fa also
 * return a preview synthesized WITH the new voice at clone time
 * ({preview_b64, preview_mime:"audio/wav"}); older backends return only
 * {voice_id} — the preview fields stay null and callers behave exactly as
 * before (additive contract).
 */
data class QwenCloneResult(
    val voiceId: String,
    val previewB64: String? = null,
    val previewMime: String? = null,
)

/** Parse POST /qwen/voices/clone → {voice_id, preview_b64?, preview_mime?}. */
internal fun parseQwenCloneResponse(raw: String): QwenCloneResult {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    return QwenCloneResult(
        voiceId = o["voice_id"]?.jsonPrimitive?.contentOrNull ?: "",
        previewB64 = o["preview_b64"]?.jsonPrimitive?.contentOrNull,
        previewMime = o["preview_mime"]?.jsonPrimitive?.contentOrNull,
    )
}

/** GET /xai/voices → gating (configured=false hides the zone) + cloned voices. */
data class XaiVoicesResult(val configured: Boolean, val voices: List<XaiVoice>)

/** Top-level so the offline unit test can exercise the wire contract directly. */
internal fun parseXaiVoicesResponse(raw: String): XaiVoicesResult {
    val j = Json { ignoreUnknownKeys = true; isLenient = true }
    val o = j.parseToJsonElement(raw).jsonObject
    val configured = o["configured"]?.jsonPrimitive?.content?.toBoolean() ?: false
    val voices = (o["voices"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
        val vo = el.jsonObject
        val id = vo["voice_id"]?.jsonPrimitive?.contentOrNull
            ?: vo["id"]?.jsonPrimitive?.contentOrNull
            ?: return@mapNotNull null
        XaiVoice(
            voiceId = id,
            name = vo["name"]?.jsonPrimitive?.contentOrNull ?: id,
        )
    }
    return XaiVoicesResult(configured, voices)
}

class VoiceLabRepository(private val api: BlackBoxApi) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    // -------------------------------------------------------------------------
    // GET /elevenlabs/status
    // -------------------------------------------------------------------------
    suspend fun fetchStatus(): ElevenLabsStatus {
        val raw = api.get("/elevenlabs/status")
        val o = json.parseToJsonElement(raw).jsonObject
        val features = o["features"]?.jsonObject
        return ElevenLabsStatus(
            configured = o["configured"]?.jsonPrimitive?.content?.toBoolean() ?: false,
            tier = o["tier"]?.jsonPrimitive?.content ?: "",
            instantVoiceCloning = features?.get("instant_voice_cloning")
                ?.jsonPrimitive?.content?.toBoolean() ?: false,
        )
    }

    // -------------------------------------------------------------------------
    // GET /elevenlabs/voices
    // -------------------------------------------------------------------------
    suspend fun fetchVoices(): VoiceLists {
        val raw = api.get("/elevenlabs/voices")
        val o = json.parseToJsonElement(raw).jsonObject
        return VoiceLists(
            myVoices = parseVoiceArray(o["my_voices"]?.jsonArray),
            premade = parseVoiceArray(o["premade"]?.jsonArray),
        )
    }

    private fun parseVoiceArray(arr: JsonArray?): List<ElevenVoice> =
        (arr ?: JsonArray(emptyList())).mapNotNull { el ->
            val vo = el.jsonObject
            val id = vo["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
            ElevenVoice(
                id = id,
                name = vo["name"]?.jsonPrimitive?.content ?: id,
                description = vo["description"]?.jsonPrimitive?.content ?: "",
                previewUrl = vo["preview_url"]?.jsonPrimitive?.content ?: "",
                category = vo["category"]?.jsonPrimitive?.content ?: "",
            )
        }

    // -------------------------------------------------------------------------
    // POST /elevenlabs/voices/clone  (multipart)
    //   name, files (1+ audio parts), consent="true",
    //   optional description, remove_background_noise
    // -------------------------------------------------------------------------
    suspend fun cloneVoice(
        name: String,
        files: List<File>,
        consent: Boolean,
        description: String = "",
        removeBackgroundNoise: Boolean = false,
    ): CloneResult {
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("name", name)
            .addFormDataPart("consent", if (consent) "true" else "false")
        if (description.isNotBlank()) builder.addFormDataPart("description", description)
        builder.addFormDataPart("remove_background_noise", removeBackgroundNoise.toString())
        files.forEach { f ->
            builder.addFormDataPart(
                "files",
                f.name,
                f.asRequestBody(mediaTypeFor(f.name)),
            )
        }
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/elevenlabs/voices/clone")
            .header("X-BlackBox-Client", "native-android/1.0")
            .post(builder.build())
            .build()

        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            return CloneResult(
                voiceId = o["voice_id"]?.jsonPrimitive?.content ?: "",
                requiresVerification = o["requires_verification"]
                    ?.jsonPrimitive?.content?.toBoolean() ?: false,
            )
        }
    }

    // -------------------------------------------------------------------------
    // POST /elevenlabs/voices/design  (json {voice_description, text?})
    // -------------------------------------------------------------------------
    suspend fun designVoice(voiceDescription: String, text: String = ""): DesignResult {
        val payload = buildJsonObject {
            put("voice_description", voiceDescription)
            if (text.isNotBlank()) put("text", text)
        }.toString()
        val raw = postOrThrow("/elevenlabs/voices/design", payload)
        val o = json.parseToJsonElement(raw).jsonObject
        val previews = (o["previews"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
            val po = el.jsonObject
            val gid = po["generated_voice_id"]?.jsonPrimitive?.content ?: return@mapNotNull null
            DesignPreview(
                generatedVoiceId = gid,
                audioUrl = po["audio_url"]?.jsonPrimitive?.content ?: "",
                durationSecs = po["duration_secs"]?.jsonPrimitive?.content?.toDoubleOrNull() ?: 0.0,
                language = po["language"]?.jsonPrimitive?.content ?: "",
            )
        }
        return DesignResult(
            text = o["text"]?.jsonPrimitive?.content ?: "",
            previews = previews,
        )
    }

    // -------------------------------------------------------------------------
    // POST /elevenlabs/voices/design/save  (json {generated_voice_id, name, description})
    // -------------------------------------------------------------------------
    suspend fun saveDesignedVoice(
        generatedVoiceId: String,
        name: String,
        description: String = "",
    ): String {
        val payload = buildJsonObject {
            put("generated_voice_id", generatedVoiceId)
            put("name", name)
            put("description", description)
        }.toString()
        val raw = postOrThrow("/elevenlabs/voices/design/save", payload)
        val o = json.parseToJsonElement(raw).jsonObject
        return o["voice_id"]?.jsonPrimitive?.content ?: ""
    }

    // -------------------------------------------------------------------------
    // DELETE /elevenlabs/voices/{voice_id}
    // -------------------------------------------------------------------------
    suspend fun deleteVoice(voiceId: String): DeleteResult {
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/elevenlabs/voices/$voiceId")
            .header("X-BlackBox-Client", "native-android/1.0")
            .delete()
            .build()
        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            val inUse = (o["in_use"]?.jsonArray ?: JsonArray(emptyList()))
                .mapNotNull { it.jsonPrimitive.content }
            return DeleteResult(
                ok = o["ok"]?.jsonPrimitive?.content?.toBoolean() ?: false,
                inUse = inUse,
            )
        }
    }

    // -------------------------------------------------------------------------
    // GET /elevenlabs/library?search=&page_size=  → community voice library
    //   No key on the backend → {voices:[], has_more:false} (gated by status).
    // -------------------------------------------------------------------------
    suspend fun searchLibrary(query: String, pageSize: Int = 30): List<SharedVoice> {
        val q = URLEncoder.encode(query.trim(), "UTF-8")
        val raw = api.get("/elevenlabs/library?search=$q&page_size=$pageSize")
        val o = json.parseToJsonElement(raw).jsonObject
        return (o["voices"]?.jsonArray ?: JsonArray(emptyList())).mapNotNull { el ->
            val vo = el.jsonObject
            val owner = vo["public_owner_id"]?.jsonPrimitive?.contentOrNull
            val vid = vo["voice_id"]?.jsonPrimitive?.contentOrNull
            // Both coordinates are required to add the voice; skip rows missing either.
            if (owner.isNullOrBlank() || vid.isNullOrBlank()) return@mapNotNull null
            SharedVoice(
                publicOwnerId = owner,
                voiceId = vid,
                name = vo["name"]?.jsonPrimitive?.contentOrNull ?: vid,
                previewUrl = vo["preview_url"]?.jsonPrimitive?.contentOrNull ?: "",
                accent = vo["accent"]?.jsonPrimitive?.contentOrNull ?: "",
                gender = vo["gender"]?.jsonPrimitive?.contentOrNull ?: "",
                age = vo["age"]?.jsonPrimitive?.contentOrNull ?: "",
                description = vo["description"]?.jsonPrimitive?.contentOrNull ?: "",
            )
        }
    }

    // -------------------------------------------------------------------------
    // POST /elevenlabs/library/add  (json {public_owner_id, voice_id, name})
    //   → {ok, voice_id}. Backend busts the voice cache on success.
    // -------------------------------------------------------------------------
    suspend fun addLibraryVoice(publicOwnerId: String, voiceId: String, name: String): String {
        val payload = buildJsonObject {
            put("public_owner_id", publicOwnerId)
            put("voice_id", voiceId)
            put("name", name)
        }.toString()
        val raw = postOrThrow("/elevenlabs/library/add", payload)
        val o = json.parseToJsonElement(raw).jsonObject
        return o["voice_id"]?.jsonPrimitive?.contentOrNull ?: ""
    }

    // -------------------------------------------------------------------------
    // xAI (Grok) custom voices — GET/POST/DELETE /xai/voices
    //   Cloned ids are Grok SESSION voices (not TTS-picker voices). The clone
    //   path mirrors cloneVoice()'s manual multipart; the backend consent gate
    //   422s unless consent == "true".
    // -------------------------------------------------------------------------
    suspend fun fetchXaiVoices(): XaiVoicesResult =
        parseXaiVoicesResponse(api.get("/xai/voices"))

    suspend fun cloneXaiVoice(
        name: String,
        file: File,
        consent: Boolean,
        description: String = "",
    ): String {
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("name", name)
            .addFormDataPart("consent", if (consent) "true" else "false")
        if (description.isNotBlank()) builder.addFormDataPart("description", description)
        builder.addFormDataPart("file", file.name, file.asRequestBody(mediaTypeFor(file.name)))
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/xai/voices")
            .header("X-BlackBox-Client", "native-android/1.0")
            .post(builder.build())
            .build()
        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            return o["voice_id"]?.jsonPrimitive?.contentOrNull ?: ""
        }
    }

    suspend fun deleteXaiVoice(voiceId: String): Boolean {
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/xai/voices/$voiceId")
            .header("X-BlackBox-Client", "native-android/1.0")
            .delete()
            .build()
        api.getClient().newCall(request).execute().use { response ->
            val body = response.body?.string() ?: ""
            if (!response.isSuccessful) {
                throw VoiceLabException(response.code, extractError(body, response.code))
            }
            val o = json.parseToJsonElement(body).jsonObject
            return o["ok"]?.jsonPrimitive?.content?.toBoolean() ?: false
        }
    }

    // -------------------------------------------------------------------------
    // On-Box (Qwen3-TTS) — status gate + clone/design/manage. Mirrors the
    // ElevenLabs helpers above; the backend routes proxy the on-box qwen-tts
    // member (see Orchestrator/routes/tts_routes.py "Qwen3-TTS voice management").
    // -------------------------------------------------------------------------

    /** GET /local-models/status → healthy + tts capability gate for the zone. */
    suspend fun fetchQwenStatus(): QwenTtsStatus =
        parseQwenStatusResponse(api.get("/local-models/status"))

    /** GET /qwen/voices → saved on-box clone/design profiles. */
    suspend fun fetchQwenVoices(): List<QwenVoice> =
        parseQwenVoicesResponse(api.get("/qwen/voices"))

    /**
     * POST /qwen/voices/clone (multipart: name, consent, description?, files).
     * The proxy 422s without the literal consent flag (ElevenLabs-gate parity).
     * Returns the new voice_id/slug plus, on servers >= ba81b8fa, a base64 WAV
     * preview synthesized with the new voice (null on older backends).
     */
    suspend fun cloneQwenVoice(
        name: String,
        files: List<File>,
        consent: Boolean,
        description: String = "",
    ): QwenCloneResult {
        val builder = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("name", name)
            .addFormDataPart("consent", if (consent) "true" else "false")
        if (description.isNotBlank()) builder.addFormDataPart("description", description)
        files.forEach { f ->
            builder.addFormDataPart("files", f.name, f.asRequestBody(mediaTypeFor(f.name)))
        }
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/qwen/voices/clone")
            .header("X-BlackBox-Client", "native-android/1.0")
            .post(builder.build())
            .build()
        // Main-safe: the VM launches on Main; a bare execute() there throws
        // NetworkOnMainThreadException (null message -> the bare "Clone failed"
        // toast Brandon hit 2026-07-23) BEFORE any request leaves the phone.
        return withContext(Dispatchers.IO) {
            api.getClient().newCall(request).execute().use { response ->
                val body = response.body?.string() ?: ""
                if (!response.isSuccessful) {
                    throw VoiceLabException(response.code, extractError(body, response.code))
                }
                parseQwenCloneResponse(body)
            }
        }
    }

    /** POST /qwen/voices/design {voice_description, text?} → preview candidates. */
    suspend fun designQwenVoice(voiceDescription: String, text: String = ""): List<QwenDesignPreview> {
        val payload = buildJsonObject {
            put("voice_description", voiceDescription)
            if (text.isNotBlank()) put("text", text)
        }.toString()
        return parseQwenDesignResponse(postOrThrow("/qwen/voices/design", payload))
    }

    /** POST /qwen/voices/design/save {generated_voice_id, name} → voice_id. */
    suspend fun saveQwenDesignedVoice(generatedVoiceId: String, name: String): String {
        val payload = buildJsonObject {
            put("generated_voice_id", generatedVoiceId)
            put("name", name)
        }.toString()
        return parseQwenVoiceIdResponse(postOrThrow("/qwen/voices/design/save", payload))
    }

    /** DELETE /qwen/voices/{slug} → ok. 404s (no such profile) surface as VoiceLabException. */
    suspend fun deleteQwenVoice(slug: String): Boolean {
        val request = Request.Builder()
            .url("${api.getBaseUrl()}/qwen/voices/$slug")
            .header("X-BlackBox-Client", "native-android/1.0")
            .delete()
            .build()
        // Main-safe (same NetworkOnMainThreadException hazard as cloneQwenVoice).
        return withContext(Dispatchers.IO) {
            api.getClient().newCall(request).execute().use { response ->
                val body = response.body?.string() ?: ""
                if (!response.isSuccessful) {
                    throw VoiceLabException(response.code, extractError(body, response.code))
                }
                val o = json.parseToJsonElement(body).jsonObject
                o["ok"]?.jsonPrimitive?.content?.toBoolean() ?: false
            }
        }
    }

    // -------------------------------------------------------------------------
    // Helpers
    // -------------------------------------------------------------------------

    /**
     * POST JSON and surface the HTTP status on failure. BlackBoxApi.post throws a
     * generic IOException("HTTP <code>...") which would lose the body; this calls
     * the shared client directly so 422/400 detail reaches the UI.
     */
    private suspend fun postOrThrow(path: String, body: String): String {
        // Reuse BlackBoxApi.post for the happy path; rewrap its IOException with
        // the parsed status code so the ViewModel can branch on 4xx.
        return try {
            api.post(path, body)
        } catch (e: IOException) {
            val code = Regex("HTTP (\\d+)").find(e.message ?: "")?.groupValues?.getOrNull(1)?.toIntOrNull() ?: 0
            throw VoiceLabException(code, e.message ?: "Request failed")
        }
    }

    private fun extractError(body: String, code: Int): String {
        return try {
            val o = json.parseToJsonElement(body).jsonObject
            o["detail"]?.jsonPrimitive?.content
                ?: o["error"]?.jsonPrimitive?.content
                ?: "HTTP $code"
        } catch (_: Exception) {
            if (body.isNotBlank()) body.take(180) else "HTTP $code"
        }.also { Log.w(TAG, "Request failed ($code): $it") }
    }

    private fun mediaTypeFor(fileName: String) =
        when (fileName.substringAfterLast('.', "").lowercase()) {
            "m4a" -> "audio/m4a"
            "mp3" -> "audio/mpeg"
            "wav" -> "audio/wav"
            "ogg" -> "audio/ogg"
            "flac" -> "audio/flac"
            "webm" -> "audio/webm"
            else -> "application/octet-stream"
        }.toMediaType()
}
