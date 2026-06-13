package com.aiblackbox.portal.data.repository

import android.util.Log
import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
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
