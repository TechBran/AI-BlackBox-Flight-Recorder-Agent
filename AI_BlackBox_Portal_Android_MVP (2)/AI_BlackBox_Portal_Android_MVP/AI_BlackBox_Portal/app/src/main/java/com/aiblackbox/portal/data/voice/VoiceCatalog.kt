package com.aiblackbox.portal.data.voice

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

data class VoiceCatalogOption(val id: String, val label: String)

/**
 * Hydrated live-provider catalog parsed from GET /realtime/status,
 * /gemini-live/status, /grok-live/status (VoiceBackend.statusPath).
 * Tolerant: every field optional; models accepted as ["id",...] OR
 * [{"id":..,"label"/"name":..},...]. Pure JVM — no android.util.Log,
 * never throws (unit-testable without Robolectric).
 */
data class VoiceCatalog(
    val models: List<VoiceCatalogOption> = emptyList(),
    val voices: List<String> = emptyList(),
    val modelDefault: String? = null,
    val presets: List<VoiceCatalogOption> = emptyList(),
) {
    companion object {
        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        fun parse(raw: String): VoiceCatalog? = try {
            val obj = json.parseToJsonElement(raw).jsonObject
            val models = obj["models"]?.jsonArray?.mapNotNull { el ->
                try {
                    val o = el.jsonObject
                    val id = o["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                    VoiceCatalogOption(
                        id,
                        o["label"]?.jsonPrimitive?.content
                            ?: o["name"]?.jsonPrimitive?.content ?: id
                    )
                } catch (_: Exception) {
                    try { VoiceCatalogOption(el.jsonPrimitive.content, el.jsonPrimitive.content) }
                    catch (_: Exception) { null }
                }
            }.orEmpty()
            val voices = obj["voices"]?.jsonArray?.mapNotNull {
                try { it.jsonPrimitive.content } catch (_: Exception) { null }
            }.orEmpty()
            val modelDefault = try {
                obj["model_default"]?.jsonPrimitive?.content?.takeIf { it.isNotBlank() }
            } catch (_: Exception) { null }
            val presets = obj["presets"]?.jsonArray?.mapNotNull { el ->
                try {
                    val o = el.jsonObject
                    val id = o["id"]?.jsonPrimitive?.content ?: return@mapNotNull null
                    VoiceCatalogOption(
                        id,
                        o["name"]?.jsonPrimitive?.content
                            ?: o["label"]?.jsonPrimitive?.content ?: id
                    )
                } catch (_: Exception) { null }
            }.orEmpty()
            VoiceCatalog(models, voices, modelDefault, presets)
        } catch (_: Exception) {
            null
        }
    }
}

/** Catalog voices when hydrated + non-empty, else the Constants fallback. */
fun VoiceCatalog?.voicesOrFallback(fallback: List<String>): List<String> =
    this?.voices?.takeIf { it.isNotEmpty() } ?: fallback

/** Catalog models as (id, label) when hydrated + non-empty, else the Constants fallback. */
fun VoiceCatalog?.modelsOrFallback(fallback: List<Pair<String, String>>): List<Pair<String, String>> =
    this?.models?.takeIf { it.isNotEmpty() }?.map { it.id to it.label } ?: fallback
