package com.aiblackbox.portal.data.model

import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable

/**
 * Subset of the backend's GET /embeddings/status response needed by the
 * updates-panel notification card (parity with the web Portal's card in
 * Portal/modules/updates-manager.js).
 * See Orchestrator/routes/embeddings_routes.py:embeddings_status().
 *
 * The full response also carries active/stores/models/ollama blocks — the
 * lenient Json config (ignoreUnknownKeys) drops them; the card only needs
 * health + job.
 */
@Serializable
data class EmbeddingsStatus(
    val health: EmbeddingsHealth = EmbeddingsHealth(),
    val job: EmbeddingsJob? = null,
)

/** Watcher health state: "ok" | "superseded" | "broken". */
@Serializable
data class EmbeddingsHealth(
    val state: String = "ok",
    val detail: String = "",
    val successor: String? = null,
    @SerialName("successor_slug") val successorSlug: String? = null,
)

/**
 * Live migration job; null when idle. state is "running" while a migration
 * is in flight ("done"/"stalled" afterwards — the card only renders progress
 * for "running"; the wizard owns stalled).
 */
@Serializable
data class EmbeddingsJob(
    val state: String = "",
    val done: Int = 0,
    val total: Int = 0,
    @SerialName("cancel_requested") val cancelRequested: Boolean = false,
)

/** Body for POST /embeddings/migrate. */
@Serializable
data class EmbeddingsMigrateRequest(
    val target: String,
)
