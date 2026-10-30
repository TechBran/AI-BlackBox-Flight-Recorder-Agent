package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.SSEClient
import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.model.ChatMessage
import com.aiblackbox.portal.data.location.UserLocation
import com.aiblackbox.portal.data.model.HealthResponse
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.StreamRequest
import com.aiblackbox.portal.data.model.TaskResponse
import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put
import java.io.File

/**
 * The native chat's calls to the box. Its turns (/chat/stream, /chat) ask for the Health origin
 * proof (signHealth): on the enrolled phone, for the enrolled operator, they are a credentialed
 * surface (spec §6.1 "Chat"), and this chat shows a health turn as one (the footer, Show
 * numbers). The interceptor decides; for every other phone and operator nothing changes.
 */
class ChatRepository(private val api: BlackBoxApi) {

    private val sseClient = SSEClient(api)

    /**
     * Send a streaming chat request via SSE.
     * Returns Flow of SSE events for real-time processing.
     */
    fun sendStream(
        userMessage: String,
        history: List<ChatMessage>,
        operator: String,
        provider: String? = null,
        model: String? = null,
        sessionId: String? = null,
        deviceId: String? = null,
        camera: String? = null,
        originDeviceId: String? = null,
        location: UserLocation? = null
    ): Flow<SSEEvent> {
        val messages = history + ChatMessage(
            role = "user",
            content = JsonPrimitive(userMessage)
        )
        val request = StreamRequest(
            messages = messages,
            operator = operator,
            provider = provider,
            model = model,
            sessionId = sessionId,
            deviceId = deviceId,
            camera = camera,
            originDeviceId = originDeviceId,
            location = location
        )
        val body = api.json.encodeToString(StreamRequest.serializer(), request)
        return sseClient.stream("/chat/stream", body, signHealth = true)
    }

    /**
     * Send a streaming chat request with multimodal content (images, audio,
     * video, documents). Each uploaded URL becomes the part its type needs
     * ([attachmentPart]).
     */
    fun sendStreamMultimodal(
        textContent: String,
        attachmentUrls: List<String>,
        history: List<ChatMessage>,
        operator: String,
        provider: String? = null,
        model: String? = null,
        sessionId: String? = null,
        deviceId: String? = null,
        camera: String? = null,
        originDeviceId: String? = null,
        location: UserLocation? = null
    ): Flow<SSEEvent> {
        val userMsg = ChatMessage(role = "user", content = multimodalUserContent(textContent, attachmentUrls))
        val messages = history + userMsg
        val request = StreamRequest(
            messages = messages,
            operator = operator,
            provider = provider,
            model = model,
            sessionId = sessionId,
            deviceId = deviceId,
            camera = camera,
            originDeviceId = originDeviceId,
            location = location
        )
        val body = api.json.encodeToString(StreamRequest.serializer(), request)
        return sseClient.stream("/chat/stream", body, signHealth = true)
    }

    /**
     * Send async chat (returns task_id for polling).
     */
    suspend fun sendAsync(
        userMessage: String,
        history: List<ChatMessage>,
        operator: String,
        provider: String? = null,
        model: String? = null
    ): TaskResponse {
        val messages = history + ChatMessage(
            role = "user",
            content = JsonPrimitive(userMessage)
        )
        val request = StreamRequest(
            messages = messages,
            operator = operator,
            provider = provider,
            model = model
        )
        val body = api.json.encodeToString(StreamRequest.serializer(), request)
        val response = api.post("/chat", body, signHealth = true)
        return api.json.decodeFromString(TaskResponse.serializer(), response)
    }

    /**
     * Save conversation turn (triggers auto-mint snapshot).
     */
    suspend fun saveConversation(request: SaveRequest): String {
        val body = api.json.encodeToString(SaveRequest.serializer(), request)
        return api.post("/chat/save", body)
    }

    /**
     * Get server health status.
     */
    suspend fun getHealth(): HealthResponse {
        val response = api.get("/health")
        return api.json.decodeFromString(HealthResponse.serializer(), response)
    }

    /**
     * Get available models for a provider.
     */
    suspend fun getModels(provider: String): String {
        return api.get("/models/$provider")
    }
}

// =============================================================================
// Attachments
// =============================================================================

private val IMAGE_EXTENSIONS = setOf("png", "jpg", "jpeg", "webp", "gif", "bmp", "heic", "heif")
private val AUDIO_EXTENSIONS = setOf(
    "wav", "mp3", "m4a", "aac", "ogg", "oga", "opus", "flac", "aiff", "aif", "amr", "mpga",
)
// Containers that carry either are typed as video: the auto-route sends both to
// Gemini, and a phone's .mp4/.webm/.3gp attachment is almost always a clip.
private val VIDEO_EXTENSIONS = setOf("mp4", "m4v", "mov", "mkv", "avi", "webm", "3gp", "mpeg", "mpg")

/**
 * The content-part type for an uploaded attachment URL, from its extension:
 * `image_url`, `audio_url`, `video_url`, or `document_url` for documents and
 * any other file. /upload keeps the file's extension, and [uploadFileName]
 * makes sure it reflects the attachment's MIME type.
 *
 * Every attachment used to go out as `image_url`, so the OpenAI and Anthropic
 * paths read a .wav or a PDF as a JPEG, and the backend's media auto-route,
 * which keys on these types to move audio and video to a model that can read
 * them, never fired for a phone attachment (audit W13).
 */
fun attachmentPartType(url: String): String {
    val ext = url.substringBefore('#').substringBefore('?')
        .substringAfterLast('/').substringAfterLast('.', "").lowercase()
    return when (ext) {
        in IMAGE_EXTENSIONS -> "image_url"
        in AUDIO_EXTENSIONS -> "audio_url"
        in VIDEO_EXTENSIONS -> "video_url"
        else -> "document_url"
    }
}

/** One content part for an uploaded attachment: `{"type": T, T: {"url": url}}`. */
fun attachmentPart(url: String): JsonObject {
    val type = attachmentPartType(url)
    return buildJsonObject {
        put("type", type)
        put(type, buildJsonObject { put("url", url) })
    }
}

/** The user turn's content: the text part, then one typed part per attachment. */
fun multimodalUserContent(textContent: String, attachmentUrls: List<String>): JsonArray =
    buildJsonArray {
        add(buildJsonObject {
            put("type", "text")
            put("text", textContent)
        })
        for (url in attachmentUrls) add(attachmentPart(url))
    }

private val EXTENSION_BY_MIME = mapOf(
    "image/jpeg" to "jpg", "image/png" to "png", "image/webp" to "webp", "image/gif" to "gif",
    "image/bmp" to "bmp", "image/heic" to "heic", "image/heif" to "heif",
    "audio/wav" to "wav", "audio/x-wav" to "wav", "audio/wave" to "wav", "audio/mpeg" to "mp3",
    "audio/mp4" to "m4a", "audio/x-m4a" to "m4a", "audio/aac" to "aac", "audio/ogg" to "ogg",
    "audio/opus" to "opus", "audio/flac" to "flac", "audio/amr" to "amr",
    "video/mp4" to "mp4", "video/quicktime" to "mov", "video/webm" to "webm",
    "video/3gpp" to "3gp", "video/x-matroska" to "mkv",
    "application/pdf" to "pdf", "text/plain" to "txt", "text/markdown" to "md", "text/csv" to "csv",
    "application/msword" to "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document" to "docx",
)

/**
 * The file name to upload an attachment under: its display name, plus the
 * extension its MIME type implies when the name has none. /upload names the
 * stored file by that extension (".tmp" without one), and the chat part is
 * typed from it ([attachmentPartType]).
 */
fun uploadFileName(displayName: String, mimeType: String): String {
    val name = displayName.ifBlank { "file" }
    if (name.substringAfterLast('.', "").isNotEmpty()) return name
    val ext = EXTENSION_BY_MIME[mimeType.substringBefore(';').trim().lowercase()] ?: return name
    return "$name.$ext"
}

/**
 * Transcribe a recording /upload already stored, by its URL, so the phone
 * sends the bytes once (audit W13: every take went up twice, to /upload and
 * again to /stt). A box that predates the url form answers 422 (`file` is
 * required there) and gets the file itself, as before; so does a take whose
 * upload failed (no URL).
 *
 * @return the /stt response body
 */
suspend fun transcribeUploadedRecording(api: BlackBoxApi, uploadedUrl: String?, file: File): String {
    if (!uploadedUrl.isNullOrBlank()) {
        try {
            return api.postForm("/stt", mapOf("url" to uploadedUrl))
        } catch (e: ApiHttpException) {
            if (e.code != 422) throw e
        }
    }
    return api.uploadFile("/stt", file)
}
