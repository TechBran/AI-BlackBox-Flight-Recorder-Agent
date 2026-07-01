package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.SSEClient
import com.aiblackbox.portal.data.api.SSEEvent
import com.aiblackbox.portal.data.model.ChatMessage
import com.aiblackbox.portal.data.model.HealthResponse
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.StreamRequest
import com.aiblackbox.portal.data.model.TaskResponse
import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

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
        originDeviceId: String? = null
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
            originDeviceId = originDeviceId
        )
        val body = api.json.encodeToString(StreamRequest.serializer(), request)
        return sseClient.stream("/chat/stream", body)
    }

    /**
     * Send a streaming chat request with multimodal content (images, files).
     */
    fun sendStreamMultimodal(
        textContent: String,
        imageUrls: List<String>,
        history: List<ChatMessage>,
        operator: String,
        provider: String? = null,
        model: String? = null,
        sessionId: String? = null,
        deviceId: String? = null,
        camera: String? = null,
        originDeviceId: String? = null
    ): Flow<SSEEvent> {
        val contentArray = buildJsonArray {
            add(buildJsonObject {
                put("type", "text")
                put("text", textContent)
            })
            for (url in imageUrls) {
                add(buildJsonObject {
                    put("type", "image_url")
                    put("image_url", buildJsonObject { put("url", url) })
                })
            }
        }

        val userMsg = ChatMessage(role = "user", content = contentArray)
        val messages = history + userMsg
        val request = StreamRequest(
            messages = messages,
            operator = operator,
            provider = provider,
            model = model,
            sessionId = sessionId,
            deviceId = deviceId,
            camera = camera,
            originDeviceId = originDeviceId
        )
        val body = api.json.encodeToString(StreamRequest.serializer(), request)
        return sseClient.stream("/chat/stream", body)
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
        val response = api.post("/chat", body)
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
