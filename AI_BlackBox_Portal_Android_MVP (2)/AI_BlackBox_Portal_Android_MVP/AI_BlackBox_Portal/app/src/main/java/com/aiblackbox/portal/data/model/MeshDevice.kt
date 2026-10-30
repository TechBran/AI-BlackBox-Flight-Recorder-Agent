package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive

/**
 * One row of the tailnet mesh join served by `GET /devices/mesh` (M3 §5.5 decision 8):
 * a tailnet node annotated with registry ownership. Consumed by the System-Menu
 * "Devices" view for operator↔device assignment, primary selection, and per-device
 * default-provider routing.
 *
 * `owner` / `defaultProvider` are nullable: an un-claimed tailnet node has no owner
 * and no provider yet. Field names mirror the backend contract exactly.
 */
@Serializable
data class MeshDevice(
    val id: String = "",
    val name: String = "",
    val tailnet: String? = null,
    val type: String = "",
    val online: Boolean = false,
    val owner: String? = null,
    @SerialName("is_primary") val isPrimary: Boolean = false,
    @SerialName("default_provider") val defaultProvider: String? = null,
    // (M7) Additive and OWNER-ONLY: the box fills these only when the request's ?operator=
    // equals this row's owner, so the badge never tells one operator about another's health
    // enrollment (rule W17). Absent = false / null, which an older box also yields.
    @SerialName("health_source") val healthSource: Boolean = false,
    @SerialName("health_state") val healthState: String? = null,
) {
    /** True once an operator has claimed this device (primary/provider need an owner). */
    val isClaimed: Boolean get() = !owner.isNullOrBlank()

    /** "Use for health" is offered on [operator]'s own Android rows that are not the source yet. */
    fun canUseForHealth(operator: String): Boolean =
        type == "android" && operator.isNotBlank() && owner == operator && !healthSource
}

/**
 * (M7) The mesh fetch scoped to the viewer (`?operator=<me>`) is the only one that carries the
 * health fields. When the list on screen is the whole tailnet, copy them onto it from the
 * scoped fetch, for rows the viewer owns and only those. PURE.
 */
fun mergeHealthFields(all: List<MeshDevice>, scoped: List<MeshDevice>, viewer: String): List<MeshDevice> {
    if (viewer.isBlank()) return all
    val byId = scoped.filter { it.owner == viewer }.associateBy { it.id }
    return all.map { d ->
        val s = byId[d.id]
        if (s == null || d.owner != viewer) d.copy(healthSource = false, healthState = null)
        else d.copy(healthSource = s.healthSource, healthState = s.healthState)
    }
}

@Serializable
data class MeshDevicesResponse(
    val devices: List<MeshDevice> = emptyList(),
    val operator: String? = null,
)

/** The valid default-provider choices (mirrors backend VALID_DEFAULT_PROVIDERS). `null`
 *  clears the override and falls back to the box default. Kept here so the picker and
 *  any tests share one source. */
val MESH_PROVIDER_CHOICES: List<String> = listOf("gemma", "gemini", "claude", "openai")

private val meshJson = Json {
    ignoreUnknownKeys = true
    isLenient = true
}

/**
 * Pure parse of a `/devices/mesh` response body into the device list. Tolerant of
 * unknown keys and malformed input (returns an empty list rather than throwing) so a
 * transient/garbled response degrades to the empty state instead of crashing the VM.
 * PURE (JVM-testable) — no Android dependencies.
 */
fun parseMeshDevices(raw: String): List<MeshDevice> = try {
    meshJson.decodeFromString(MeshDevicesResponse.serializer(), raw).devices
} catch (_: Exception) {
    emptyList()
}

/**
 * Pure parse of a `GET /operators` response body into the operator roster. The endpoint
 * returns a STRING array: `{"operators":["Brandon","Anna"]}` — NOT a list of objects, so
 * the old `it.jsonObject["operator"]` path threw on every `JsonPrimitive` (leaving the
 * owner dropdown permanently empty). Reads the strings directly, drops blanks, and
 * tolerates unknown keys / malformed input (returns an empty list rather than throwing).
 * PURE (JVM-testable) — no Android dependencies.
 */
fun parseOperators(raw: String): List<String> = try {
    meshJson.parseToJsonElement(raw).jsonObject["operators"]
        ?.jsonArray?.mapNotNull { it.jsonPrimitive.contentOrNull }
        ?.filter { it.isNotBlank() } ?: emptyList()
} catch (_: Exception) {
    emptyList()
}
