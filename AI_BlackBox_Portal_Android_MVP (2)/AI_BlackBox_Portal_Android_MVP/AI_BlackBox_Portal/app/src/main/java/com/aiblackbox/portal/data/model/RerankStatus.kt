package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Subset of the backend's GET /rerank/status response needed by the Android
 * updates-panel reranker card (surface 3/3 — parity with the M10.1 wizard
 * selector and the M11 Portal card in Portal/modules/updates-manager.js).
 * See Orchestrator/rerank.py:status() / model_catalog().
 *
 * IMPORTANT: the tier/key-gated selector is driven by [modelCatalog]
 * (status.model_catalog — the per-model metadata M10 added), NOT the flat
 * `models` slug list, which carries no provider/tiers/key_present and so can't
 * drive a gated selector. The lenient Json (ignoreUnknownKeys) drops `models`
 * and every other unknown field, so an older/newer backend never breaks decode.
 */
@Serializable
data class RerankStatus(
    val enabled: Boolean = false,
    val available: Boolean = false,
    val tier: String? = null,
    // Honest, tier-aware "which should I pick?" guidance (free-first; leads with
    // the local reranker on capable hardware). Rendered under the card lede.
    @SerialName("tier_guidance") val tierGuidance: String? = null,
    val provider: String? = null,
    val model: String? = null,
    // Local-vllm readiness gate (mirrors the Portal card's `rr.gpu &&
    // rr.service_reachable`): a HIGH-tier vLLM option is only selectable when a
    // GPU is present AND the reranker service answers on base_url.
    val gpu: Boolean = false,
    @SerialName("service_reachable") val serviceReachable: Boolean = false,
    // The *selected* model's key presence (top-level). Per-model key gating uses
    // each catalog entry's own key_present.
    @SerialName("key_present") val keyPresent: Boolean = false,
    val preflight: RerankPreflight? = null,
    @SerialName("model_catalog") val modelCatalog: List<RerankModel> = emptyList(),
)

/** Subset of status.preflight — used only for the "preflight NNN ms" note. */
@Serializable
data class RerankPreflight(
    @SerialName("latency_ms") val latencyMs: Double? = null,
    val state: String? = null,
)

/**
 * One entry of status.model_catalog (Orchestrator/rerank.py:model_catalog()).
 * `key_present` is resolved fresh server-side (os.getenv(key_env)) so a paste
 * gates selectability with no restart. Vertex (gcp_service_account, key_env
 * null) is always key_present=false → the UI treats it as "Advanced".
 */
@Serializable
data class RerankModel(
    val slug: String = "",
    val provider: String = "",
    val label: String = "",
    val tiers: List<String> = emptyList(),
    val privacy: String? = null,
    @SerialName("auth_kind") val authKind: String = "none",
    @SerialName("key_env") val keyEnv: String? = null,
    @SerialName("key_present") val keyPresent: Boolean = false,
    @SerialName("cost_note") val costNote: String = "",
    @SerialName("quality_note") val qualityNote: String = "",
)

/**
 * Body for POST /rerank/select (mirror of the backend RerankSelectRequest).
 * [apiKey] defaults to null and — because the repository's Json is configured
 * with encodeDefaults=false — is OMITTED from the serialized body when null.
 * So selecting an already-keyed provider posts {provider, model, enabled} with
 * NO api_key; only the Android paste-key path (an un-keyed Voyage/Cohere entry)
 * sends api_key, which the endpoint writes to .env + mirrors into os.environ.
 */
@Serializable
data class RerankSelectRequest(
    val provider: String,
    val model: String,
    val enabled: Boolean,
    @SerialName("api_key") val apiKey: String? = null,
)

/**
 * The per-model action the card should render, mirroring the M11 Portal
 * `optionHtml` decision tree (updates-manager.js). Pure so it is unit-tested
 * without a running ViewModel/Compose.
 *
 *  - ACTIVE             — this model is the current selection AND reranking is on.
 *  - SELECTABLE         — ready to select now (keyed cloud/LLM, in-process CPU,
 *                         or a ready local vLLM); POST carries NO api_key.
 *  - NEEDS_KEY_PASTE    — un-keyed Voyage/Cohere: Android offers a paste field;
 *                         POST carries the pasted api_key.
 *  - NEEDS_KEY_LINK     — un-keyed LLM reranker: uses an existing frontier key;
 *                         deep-link the API-Keys step (no paste here).
 *  - VERTEX_ADVANCED    — GCP service-account setup; deep-link the Portal
 *                         (SA JSON upload on mobile is disproportionate).
 *  - LOCAL_UNAVAILABLE  — a HIGH-tier local vLLM option without a running
 *                         reranker service (installer remediation).
 */
enum class RerankAction {
    ACTIVE, SELECTABLE, NEEDS_KEY_PASTE, NEEDS_KEY_LINK, VERTEX_ADVANCED, LOCAL_UNAVAILABLE
}

/** Catalog models whose `tiers` include this box's hardware tier (tier-gate). */
fun RerankStatus.tierModels(): List<RerankModel> =
    modelCatalog.filter { it.tiers.contains(tier) }

/** Resolve the render/POST action for one model against the current status. */
fun rerankActionFor(model: RerankModel, status: RerankStatus): RerankAction {
    val isActive = model.slug == status.model && status.enabled
    if (isActive) return RerankAction.ACTIVE
    return when (model.provider) {
        // Vertex: selectable once the SA is uploaded (keyPresent resolves from
        // GOOGLE_APPLICATION_CREDENTIALS on the backend); else deep-link setup.
        "vertex" ->
            if (model.keyPresent) RerankAction.SELECTABLE else RerankAction.VERTEX_ADVANCED
        "voyage", "cohere" ->
            if (model.keyPresent) RerankAction.SELECTABLE else RerankAction.NEEDS_KEY_PASTE
        "llm" ->
            if (model.keyPresent) RerankAction.SELECTABLE else RerankAction.NEEDS_KEY_LINK
        "cpu" -> RerankAction.SELECTABLE
        // vllm (HIGH) + any other local: needs the installed+running service.
        else -> if (status.gpu && status.serviceReachable)
            RerankAction.SELECTABLE else RerankAction.LOCAL_UNAVAILABLE
    }
}
