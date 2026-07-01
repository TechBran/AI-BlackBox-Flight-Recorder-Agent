package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json

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
) {
    /** True once an operator has claimed this device (primary/provider need an owner). */
    val isClaimed: Boolean get() = !owner.isNullOrBlank()
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
