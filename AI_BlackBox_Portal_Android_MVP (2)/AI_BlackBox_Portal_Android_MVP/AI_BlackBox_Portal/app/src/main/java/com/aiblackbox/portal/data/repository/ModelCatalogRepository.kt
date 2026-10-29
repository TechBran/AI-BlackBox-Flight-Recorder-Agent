package com.aiblackbox.portal.data.repository

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.ModelCatalog
import com.aiblackbox.portal.data.store.AtomicTextFile
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.longOrNull
import kotlinx.serialization.json.put
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.ConcurrentHashMap
import kotlin.coroutines.cancellation.CancellationException

private const val TAG = "ModelCatalogs"

/**
 * The box's model catalogs (GET /models/{provider}), ONE copy shared by the chat composer
 * and Settings sheet, the cron editor, the SMS reply picker and the CU screen (audit
 * A01-activities-root-10, rank 76; house rule: the provider API is the source of truth).
 *
 * Before, the Settings and SMS pickers read the hand-maintained Constants.MODEL_CONFIG,
 * which went stale (retired Claude ids 404'd until it was hand-patched) and disagreed with
 * the Portal, and the chat and cron screens each kept their own 5-minute cache.
 *
 *  - [refresh] answers from memory while a catalog is younger than [ttlMs], else asks the
 *    box; a successful answer replaces the memory copy and the [lastGood] copy on disk.
 *  - [cached] is what the pickers show at once, with no network: the memory copy, else the
 *    last-good copy from disk, so a cold start or an unreachable box still lists real
 *    models; Constants.MODEL_CONFIG is only the placeholder for a phone that has never
 *    reached its box.
 *  - custom (user-registered LAN servers) is never cached at any layer, here as on the
 *    box: servers are added in the wizard and models load and unload between two looks,
 *    so every [refresh] asks the box and [cached] is always null.
 *
 * [fetch] is GET /models/{provider} for a catalog key ([apiProviderFor]).
 */
class ModelCatalogRepository(
    private val fetch: suspend (apiProvider: String) -> String,
    private val lastGood: LastGoodCatalogStore? = null,
    private val now: () -> Long = { System.currentTimeMillis() },
    private val ttlMs: Long = TTL_MS,
) {
    private val memory = ConcurrentHashMap<String, ModelCatalog>()

    /** The freshest copy known without the network: memory, else disk; null for custom. */
    fun cached(apiProvider: String): ModelCatalog? {
        if (apiProvider == ModelCatalog.CUSTOM) return null
        memory[apiProvider]?.let { return it }
        val saved = lastGood?.read(apiProvider) ?: return null
        return ModelCatalog.parse(apiProvider, saved.body, saved.fetchedAtMs, fromBox = false)
    }

    /**
     * The catalog for [apiProvider]: the memory copy while it is younger than the TTL,
     * else the box's answer. Null when the box could not be asked or did not answer with
     * a catalog (the caller keeps showing [cached]). An empty catalog is an answer, but
     * is not kept: it would hide a provider's models for a whole TTL.
     */
    suspend fun refresh(apiProvider: String, force: Boolean = false): ModelCatalog? {
        val custom = apiProvider == ModelCatalog.CUSTOM
        if (!custom && !force) {
            memory[apiProvider]?.let { hit -> if (now() - hit.fetchedAtMs < ttlMs) return hit }
        }
        val fetchedAt = now()
        val raw = try {
            fetch(apiProvider)
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            Log.d(TAG, "GET /models/$apiProvider failed: ${e.message}")
            return null
        }
        val catalog = ModelCatalog.parse(apiProvider, raw, fetchedAt, fromBox = true) ?: return null
        if (!custom && catalog.models.isNotEmpty()) {
            memory[apiProvider] = catalog
            try {
                lastGood?.write(apiProvider, raw, fetchedAt)
            } catch (e: Exception) {
                Log.w(TAG, "last-good $apiProvider catalog not saved: ${e.message}")
            }
        }
        return catalog
    }

    /**
     * The ids of [apiProvider]'s catalog as the box lists them now (within the TTL), for
     * the provider-switch restore (ModelSelection.setProviderAndModel). Null when not
     * known: a stale disk copy must not bring back a pick the box has dropped.
     */
    suspend fun liveIds(apiProvider: String): List<String>? = refresh(apiProvider)?.ids

    companion object {
        /** The box caches a live catalog for 10 minutes; the phone asks again after 5. */
        const val TTL_MS = 5 * 60 * 1_000L

        /**
         * The box's catalog key for an app provider id; null for providers with no
         * /models endpoint (agents, voice, robotics, on-device), which keep their own
         * lists. Gemini is "google" on the box.
         */
        fun apiProviderFor(provider: String): String? = when (provider) {
            "gemini" -> "google"
            "anthropic" -> "anthropic"
            "openai" -> "openai"
            "xai" -> "xai"
            // The merged live Anthropic/Google/OpenAI CU catalogs, each entry with a backend.
            "computer-use" -> "computer-use"
            // User-registered OpenAI-compatible servers (registry roster + status).
            "custom" -> "custom"
            else -> null
        }
    }
}

/** The last catalog the box served per provider, kept so a cold start lists real models. */
interface LastGoodCatalogStore {
    data class Saved(val body: String, val fetchedAtMs: Long)

    fun read(apiProvider: String): Saved?
    fun write(apiProvider: String, body: String, fetchedAtMs: Long)
}

/**
 * [LastGoodCatalogStore] as one small JSON file per box and provider under [dir]
 * (`<sha256(origin) prefix>/<provider>.json`), written atomically (AtomicTextFile); a file
 * that no longer parses is moved aside and read as nothing.
 */
class FileLastGoodCatalogStore(private val dir: File, origin: String) : LastGoodCatalogStore {

    private val boxDir = File(dir, originKey(origin))

    private fun fileFor(apiProvider: String): File? =
        apiProvider.takeIf { SAFE_NAME.matches(it) }?.let { File(boxDir, "$it.json") }

    override fun read(apiProvider: String): LastGoodCatalogStore.Saved? {
        val file = fileFor(apiProvider)?.takeIf { it.isFile } ?: return null
        return try {
            val obj = json.parseToJsonElement(file.readText()) as JsonObject
            val body = obj["body"]?.jsonPrimitive?.contentOrNull ?: error("no body")
            val at = (obj["fetched_at_ms"] as? JsonPrimitive)?.longOrNull ?: 0L
            LastGoodCatalogStore.Saved(body, at)
        } catch (e: Exception) {
            Log.w(TAG, "last-good $apiProvider catalog unreadable, set aside: ${e.message}")
            AtomicTextFile.quarantine(file)
            null
        }
    }

    @Synchronized
    override fun write(apiProvider: String, body: String, fetchedAtMs: Long) {
        val file = fileFor(apiProvider) ?: return
        AtomicTextFile.write(file, buildJsonObject {
            put("body", body)
            put("fetched_at_ms", fetchedAtMs)
        }.toString())
    }

    private companion object {
        val json = Json { ignoreUnknownKeys = true }
        val SAFE_NAME = Regex("[a-z0-9-]{1,40}")

        fun originKey(origin: String): String =
            MessageDigest.getInstance("SHA-256").digest(origin.trim().toByteArray())
                .joinToString("") { "%02x".format(it) }.take(16)
    }
}

/**
 * The process-wide [ModelCatalogRepository] per box, so every screen shares one catalog
 * copy. [init] (PortalApplication) gives it the app's files dir for the last-good copies;
 * without it (JVM tests) catalogs live in memory only.
 */
object ModelCatalogs {
    @Volatile private var dir: File? = null
    private val byOrigin = ConcurrentHashMap<String, ModelCatalogRepository>()

    fun init(context: Context) {
        dir = File(context.applicationContext.filesDir, "model_catalogs")
    }

    fun forApi(api: BlackBoxApi): ModelCatalogRepository =
        byOrigin.getOrPut(api.getBaseUrl()) {
            ModelCatalogRepository(
                fetch = { provider -> api.get("/models/$provider") },
                lastGood = dir?.let { FileLastGoodCatalogStore(it, api.getBaseUrl()) },
            )
        }

    /** Forget every box's memory copy (tests: a reused MockWebServer port is the same box). */
    @VisibleForTesting
    internal fun clear() = byOrigin.clear()
}
