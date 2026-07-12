package com.aiblackbox.portal.data.voice

import kotlinx.serialization.Serializable
import kotlinx.serialization.builtins.ListSerializer
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject

/** One voice-agent preset from GET /voice-agents (server resolves the rest at configure). */
@Serializable
data class VoiceAgentPreset(
    val id: String,
    val name: String,
    val provider: String,   // matches VoiceBackend.id: realtime | gemini-live | grok-live
)

object VoiceAgentPresets {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** Parse {"agents":[...]} — ANY malformed input degrades to emptyList (fresh-box safe). */
    fun parse(body: String): List<VoiceAgentPreset> = try {
        val agents = json.parseToJsonElement(body).jsonObject["agents"] ?: return emptyList()
        json.decodeFromJsonElement(ListSerializer(VoiceAgentPreset.serializer()), agents)
    } catch (e: Exception) {
        emptyList()
    }
}
