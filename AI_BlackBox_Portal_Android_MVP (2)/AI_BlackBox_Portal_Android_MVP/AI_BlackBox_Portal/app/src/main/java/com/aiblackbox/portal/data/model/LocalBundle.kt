package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * One downloadable on-device Gemma model bundle, as advertised by the hub's
 * GET /local/models/catalog endpoint (mirror metadata).
 *
 * Backend shape (Orchestrator/routes/local_routes.py:local_models_catalog +
 * Orchestrator/local_provider/mirror.py): each bundle carries
 *   {slug, display_name, hf_repo, filename, size_bytes, sha256, min_ram_gb,
 *    recommended_for}.
 *
 * `size_bytes` and `sha256` are `null` until the real Hugging Face fetch fills
 * them in (Task 1.2), so both are nullable here. `min_ram_gb` is a float
 * (e.g. 3.0). The lenient Json config (ignoreUnknownKeys) tolerates any extra
 * fields the backend may add later.
 */
@Serializable
data class LocalBundle(
    val slug: String = "",
    @SerialName("display_name") val displayName: String = "",
    @SerialName("hf_repo") val hfRepo: String = "",
    val filename: String = "",
    @SerialName("size_bytes") val sizeBytes: Long? = null,
    val sha256: String? = null,
    @SerialName("min_ram_gb") val minRamGb: Double = 0.0,
    @SerialName("recommended_for") val recommendedFor: String = "",
)

/** Wrapper for GET /local/models/catalog → {"bundles": [...]}. */
@Serializable
data class LocalCatalogResponse(
    val bundles: List<LocalBundle> = emptyList(),
)

/**
 * One attested on-device model record for an operator, as returned inside
 * GET /local/device/status → {"available", "models": [<this>...]} and
 * POST /local/device/attest → {"success", "device": <this>}.
 *
 * Backend shape (Orchestrator/local_provider/registry.py:attest):
 *   {device_id, model_slug, version, sha256, delegate, autonomy_mode,
 *    verified_at}.
 */
@Serializable
data class LocalDeviceRecord(
    @SerialName("device_id") val deviceId: String = "",
    @SerialName("model_slug") val modelSlug: String? = null,
    val version: String? = null,
    val sha256: String? = null,
    val delegate: String? = null,
    @SerialName("autonomy_mode") val autonomyMode: String = "permission",
    @SerialName("verified_at") val verifiedAt: Double = 0.0,
)

/** GET /local/device/status?operator=… → {"available": bool, "models": [...]}. */
@Serializable
data class LocalStatus(
    val available: Boolean = false,
    val models: List<LocalDeviceRecord> = emptyList(),
)

/**
 * Body for POST /local/device/attest. Records which Gemma model an operator's
 * device has verified locally. `operator` + `deviceId` are required by the
 * backend; the rest are optional metadata. `autonomyMode` defaults to
 * "permission" (the backend's own default).
 */
@Serializable
data class AttestRequest(
    val operator: String,
    @SerialName("device_id") val deviceId: String,
    @SerialName("model_slug") val modelSlug: String? = null,
    val version: String? = null,
    val sha256: String? = null,
    val delegate: String? = null,
    @SerialName("autonomy_mode") val autonomyMode: String = "permission",
)

/** POST /local/device/attest → {"success": bool, "device": {...}}. */
@Serializable
data class AttestResponse(
    val success: Boolean = false,
    val device: LocalDeviceRecord? = null,
)

/**
 * Body for POST /local/device/autonomy — flips a device's autonomy posture
 * between "permission" (asks before high-consequence phone actions) and "yolo"
 * (full autonomy). `operator` + `deviceId` are required by the backend; `mode`
 * is one of those two literals.
 */
@Serializable
data class AutonomyRequest(
    val operator: String,
    @SerialName("device_id") val deviceId: String,
    val mode: String,
)

/** POST /local/device/autonomy → {"success": bool, ...}. */
@Serializable
data class AutonomyResponse(
    val success: Boolean = false,
)

/**
 * GET /local/system-prompt?operator=… → {"prompt": str, "version": str}.
 *
 * The persona / tone / anti-sycophancy system prompt the cloud chat path uses,
 * built server-side from `behavioral_core` so the on-device (`local`) provider
 * reasons with the SAME persona. `version` is a short content hash the app caches
 * alongside the prompt for staleness display / a possible future conditional
 * fetch; the current cache does NOT consult it — [PersonaCache] simply overwrites
 * the stored {prompt, version} on every successful online fetch (see PersonaCache).
 */
@Serializable
data class PersonaResponse(
    val prompt: String = "",
    val version: String = "",
)
