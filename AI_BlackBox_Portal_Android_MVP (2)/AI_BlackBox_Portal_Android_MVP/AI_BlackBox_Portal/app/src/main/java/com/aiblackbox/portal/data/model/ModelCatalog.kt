package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull

/**
 * One model of a GET /models/{provider} catalog (the box's locked `_wrap` envelope,
 * Orchestrator/routes/admin_routes.py). [backend] is on computer-use entries only
 * (anthropic | google | openai); [status] (loaded | unloaded | null) and [server] (the
 * registry alias) are on custom entries only.
 */
@Serializable
data class CatalogModelDto(
    val id: String = "",
    val name: String? = null,
    val backend: String? = null,
    val status: String? = null,
    val server: String? = null,
)

/**
 * The GET /models/{provider} envelope. [models] is kept as raw elements so one entry of
 * an odd shape is skipped instead of failing the whole catalog ([ModelCatalog.parse]);
 * null means the body had no model list at all, which is "unknown", not "empty".
 * [source] is "live" when the box read the provider's own catalog, "fallback" when it
 * served its static list (no key, upstream down). computer-use merges three vendors and
 * says how each answered in [vendorStatus] (live | fallback | error; a vendor that did
 * not answer live is backfilled with the box's static entries).
 */
@Serializable
data class ModelCatalogDto(
    val provider: String? = null,
    val models: List<JsonElement>? = null,
    val source: String? = null,
    @SerialName("default_id") val defaultId: String? = null,
    @SerialName("fetched_iso") val fetchedIso: String? = null,
    @SerialName("backends") val vendorStatus: JsonElement? = null,
)

/**
 * A provider's model catalog as the pickers use it (audit A01-activities-root-10, rank 76).
 *
 * [provider] is the box's catalog key (google, openai, anthropic, xai, custom,
 * computer-use). [fromBox] is true for a catalog fetched from the box in this process,
 * false for the last-good copy read back from disk; only a [fromBox] catalog of [source]
 * "live" is trusted to say a stored id is gone ([isAuthoritative]).
 */
data class ModelCatalog(
    val provider: String,
    val models: List<CatalogModelDto>,
    val defaultId: String?,
    val source: String?,
    val fetchedAtMs: Long,
    val fromBox: Boolean,
    val vendorStatus: Map<String, String> = emptyMap(),
) {
    val ids: List<String> get() = models.map { it.id }

    /**
     * True when the box read this list from the provider's own live catalog (every
     * vendor's, for the merged computer-use one). Otherwise some or all of it is the box's
     * static "fallback" guess, served while a provider's key or upstream is down.
     */
    val isLive: Boolean
        get() = source == "live" && vendorStatus.values.all { it == "live" }

    /**
     * True when this catalog may replace stored ids it does not list: fetched from the
     * box just now, [isLive], and not empty. A "fallback" list is the box's static guess
     * while a provider is unreachable, and a copy from disk may predate a model the box has
     * since added, so neither may.
     */
    val isAuthoritative: Boolean
        get() = fromBox && isLive && models.isNotEmpty()

    /**
     * The picker rows: Auto ("") first, then every model as (id, name). Empty when the box
     * lists no model, so the picker falls back to its placeholder. Auto names the default
     * model for computer-use and custom, whose default the box names in `default_id`
     * (mirrors the Portal's buildHydratedModels); every other provider says "Auto - Latest".
     */
    fun options(): List<Pair<String, String>> {
        if (models.isEmpty()) return emptyList()
        val named = models.map { it.id to (it.name?.takeIf { n -> n.isNotBlank() } ?: it.id) }
        val defaultName = if (provider == COMPUTER_USE || provider == CUSTOM) {
            named.firstOrNull { it.first == defaultId }?.second
        } else null
        val auto = if (defaultName != null) "Auto - $defaultName" else AUTO_LATEST
        return listOf("" to auto) + named
    }

    /**
     * computer-use only: id -> backend for CuScreen's partition, with Auto ("") in the
     * default model's backend (anthropic when the default is unknown, the box's own
     * default backend). Empty for every other provider.
     */
    fun backends(): Map<String, String> {
        if (provider != COMPUTER_USE || models.isEmpty()) return emptyMap()
        val map = models.mapNotNull { m -> m.backend?.let { m.id to it } }.toMap().toMutableMap()
        map[""] = defaultId?.let { map[it] } ?: "anthropic"
        return map
    }

    /** custom only: id -> load status ("loaded" | "unloaded"); models without one are absent. */
    fun statuses(): Map<String, String> =
        if (provider != CUSTOM) emptyMap()
        else models.mapNotNull { m -> m.status?.let { m.id to it } }.toMap()

    companion object {
        const val COMPUTER_USE = "computer-use"
        const val CUSTOM = "custom"
        const val AUTO_LATEST = "Auto - Latest"

        private val json = Json { ignoreUnknownKeys = true; isLenient = true }

        /**
         * Parse a GET /models/{provider} body. Null when it is not a catalog (not JSON, or
         * no `models` list); entries without an id, or of another shape, are skipped.
         */
        fun parse(provider: String, raw: String, fetchedAtMs: Long, fromBox: Boolean): ModelCatalog? {
            val dto = try {
                val root = json.parseToJsonElement(raw) as? JsonObject ?: return null
                json.decodeFromJsonElement(ModelCatalogDto.serializer(), root)
            } catch (_: Exception) {
                return null
            }
            val entries = dto.models ?: return null
            val models = entries.mapNotNull { el ->
                try {
                    json.decodeFromJsonElement(CatalogModelDto.serializer(), el).takeIf { it.id.isNotBlank() }
                } catch (_: Exception) {
                    null
                }
            }
            return ModelCatalog(
                provider = provider,
                models = models,
                defaultId = dto.defaultId?.takeIf { it.isNotBlank() },
                source = dto.source,
                fetchedAtMs = fetchedAtMs,
                fromBox = fromBox,
                vendorStatus = (dto.vendorStatus as? JsonObject).orEmpty()
                    .mapValues { (_, v) -> (v as? JsonPrimitive)?.contentOrNull.orEmpty() },
            )
        }
    }
}
