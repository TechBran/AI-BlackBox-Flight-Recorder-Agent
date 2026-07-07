package com.aiblackbox.portal.data.model

import kotlinx.serialization.Serializable

/**
 * Read-only subset of the backend's GET /rerank/status response needed by the
 * Android updates-panel reranker STATUS LINE. The tier/key-gated selector moved
 * to the onboarding wizard (M10.1); the updates screen now only surfaces the
 * current state — on/off, the selected provider/model, and whether the reranker
 * is actually in use (`available`, which a failed preflight flips to false).
 * See Orchestrator/rerank.py:status().
 *
 * The lenient Json (ignoreUnknownKeys — UpdateRepository.kt) drops every other
 * field (tier, model_catalog, preflight, gpu, service_reachable, …) so an
 * older/newer backend never breaks decode.
 */
@Serializable
data class RerankStatus(
    val enabled: Boolean = false,
    val available: Boolean = false,
    val provider: String? = null,
    val model: String? = null,
)
