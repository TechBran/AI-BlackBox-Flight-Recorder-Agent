package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.SerialName
import kotlinx.serialization.Serializable
import kotlinx.serialization.json.Json
import java.io.File
import java.io.IOException
import java.security.MessageDigest

/**
 * Per-model runtime config that travels with an installed bundle (Task W2),
 * mirroring Edge Gallery's per-`Model` config (`llmMaxToken`, `llmSupportImage`,
 * `configs` topK/topP/temperature). Recorded in the `<slug>.json` sidecar and
 * carried onto [InstalledModel] so the engine is configured PER MODEL instead of
 * from one hardcoded global.
 *
 * All fields are OPTIONAL/defaulted so a LEGACY sidecar (only slug/filename/
 * size_bytes) still parses: [maxTokens] null -> the engine's
 * [LiteRtEngine.DEFAULT_MAX_TOKENS]; the sampler trio null -> the engine omits
 * `samplerConfig` (uses litertlm's built-in default); [supportImage]/[recommended]
 * default false.
 *
 * @param maxTokens context window (input+output tokens) for `EngineConfig.maxNumTokens`.
 *   Null -> engine default.
 * @param supportImage whether the bundle accepts image input (W4 will wire the
 *   vision backend; W2 only CARRIES the flag).
 * @param recommended catalog/UI hint that this is the recommended model (W6 uses it).
 * @param contextNote human-readable note about the context window / model (UI hint).
 * @param topK / topP / temperature optional sampler overrides. litertlm's
 *   `SamplerConfig` requires all three together, so [LiteRtEngine] supplies its own
 *   defaults for any left null - but only builds a `SamplerConfig` at all when at
 *   least one is set (otherwise it omits it entirely, preserving prior behavior).
 */
data class ModelConfig(
    val maxTokens: Int? = null,
    val supportImage: Boolean = false,
    val recommended: Boolean = false,
    val contextNote: String? = null,
    val topK: Int? = null,
    val topP: Float? = null,
    val temperature: Float? = null,
)

/**
 * One on-device Gemma model that is present on disk.
 *
 * Returned by [LocalModelManager.installedModels] and [LocalModelManager.install].
 * `file` is the actual bundle file under the manager's `modelsDir`.
 *
 * @param config per-model runtime config (Task W2) parsed from the sidecar; legacy
 *   sidecars yield an all-default [ModelConfig].
 */
data class InstalledModel(
    val slug: String,
    val file: File,
    val sizeBytes: Long,
    val config: ModelConfig = ModelConfig(),
)

/**
 * Orchestration layer above [LocalModelDownloader] (implemented by
 * [com.aiblackbox.portal.data.api.LocalModelApi]). Owns the on-disk model
 * files: recommends E2B vs E4B by phone RAM, verifies checksums, and runs the
 * full download → verify → attest flow that makes a model usable. The Model
 * Manager UI (Task 1.5) and picker gating (Task 1.6) call into this.
 *
 * **Testable by construction.** Every Android-framework fact is a constructor
 * seam — [modelsDir], [totalRamBytes], [deviceId] — so the core unit-tests with
 * plain JUnit (no Robolectric, no Context). All framework access lives in the
 * [fromContext] factory.
 *
 * **How [installedModels] recognises files (sidecar approach).** On a
 * successful download + verify, [install] writes a tiny `<slug>.json` sidecar
 * next to the bundle recording {slug, filename, size_bytes}. [installedModels]
 * scans those sidecars and reports each one whose bundle file is still present.
 * This keeps [installedModels] hermetic and synchronous — no network catalog
 * fetch, no fragile filename→slug guessing.
 *
 * @param api downloader/attester (production: a LocalModelApi).
 * @param modelsDir where bundles + sidecars live (production:
 *   `context.filesDir.resolve("local_models")`; tests: a tmp dir).
 * @param totalRamBytes device RAM in bytes (production: ActivityManager
 *   MemoryInfo.totalMem; tests: a fixed value). Never call framework APIs here.
 * @param deviceId stable device id used in the attest record.
 */
class LocalModelManager(
    private val api: LocalModelDownloader,
    private val modelsDir: File,
    private val totalRamBytes: () -> Long,
    private val deviceId: String,
) : LocalModelInstaller {

    /**
     * Scan [modelsDir] for installed bundles via their `<slug>.json` sidecars.
     * Only sidecars whose recorded bundle file actually exists are returned
     * (a deleted/partial file is treated as not installed). Size is taken from
     * the real file length, not the recorded value, so it's always accurate.
     *
     * **Sidecar invariant:** bundle filenames must NOT end in [SIDECAR_SUFFIX]
     * (`.json`) — sidecar detection is suffix-based, so a `.json` bundle would be
     * mistaken for a sidecar. Today's `.litertlm` filenames are safe.
     *
     * Disk I/O runs on [Dispatchers.IO] so callers on the main thread can't ANR.
     */
    override suspend fun installedModels(): List<InstalledModel> = withContext(Dispatchers.IO) {
        val dir = modelsDir
        if (!dir.isDirectory) return@withContext emptyList()
        val sidecars = dir.listFiles { f -> f.isFile && f.name.endsWith(SIDECAR_SUFFIX) }
            ?: return@withContext emptyList()
        sidecars.mapNotNull { sidecar ->
            val record = runCatching {
                json.decodeFromString(SidecarRecord.serializer(), sidecar.readText())
            }.getOrNull() ?: return@mapNotNull null
            val file = File(dir, record.filename)
            if (!file.isFile) return@mapNotNull null
            InstalledModel(
                slug = record.slug,
                file = file,
                sizeBytes = file.length(),
                config = record.toModelConfig(),
            )
        }.sortedBy { it.slug }
    }

    /**
     * Pick the bundle best matched to this device's RAM, PREFERRING the catalog's
     * recommended default (Task W6: E4B — best on-device agent reliability).
     *
     * Order, among the bundles whose `minRamGb` fits in [totalRamBytes]:
     *   1. the catalog-recommended bundle if it fits (E4B on a high-RAM phone);
     *   2. otherwise the heaviest fitting bundle (a high-RAM proxy for "best");
     *   3. if NONE fit, the lightest bundle overall (so a very low-RAM phone
     *      still gets the most-likely-to-run model — E2B, the experimental one).
     *
     * Pure + testable: depends only on [totalRamBytes] and the [bundles]' own
     * `recommended`/`minRamGb` fields. The returned bundle carries its own
     * [LocalBundle.contextNote] (E4B "Recommended…", E2B "Experimental…"), so the
     * caller surfaces the right note for whichever model was chosen.
     *
     * @throws IllegalArgumentException if [bundles] is empty.
     */
    override suspend fun recommendForDevice(bundles: List<LocalBundle>): LocalBundle {
        require(bundles.isNotEmpty()) { "recommendForDevice requires a non-empty bundle list" }
        val ram = totalRamBytes()
        val fitting = bundles.filter { (it.minRamGb * BYTES_PER_GIB).toLong() <= ram }
        // 1) the catalog-recommended default (E4B) if it fits, else
        // 2) the heaviest fitting bundle, else
        // 3) the lightest bundle overall (nothing fit — most-likely-to-run).
        return fitting.firstOrNull { it.recommended }
            ?: fitting.maxByOrNull { it.minRamGb }
            ?: bundles.minByOrNull { it.minRamGb }!!
    }

    /**
     * Streamed SHA-256 of [file] equals [expectedSha256] (case-insensitive hex).
     *
     * **No-op when unknown.** If [expectedSha256] is null or blank this returns
     * `true` — the backend catalog's sha is null until the real Hugging Face
     * fetch fills it in, so there is nothing to verify against yet. This mirrors
     * the server's "skip verification when the digest is unknown" stance.
     *
     * The multi-GB streamed hash runs on [Dispatchers.IO] so callers on the main
     * thread can't ANR.
     */
    suspend fun verify(file: File, expectedSha256: String?): Boolean = withContext(Dispatchers.IO) {
        if (expectedSha256.isNullOrBlank()) return@withContext true
        if (!file.isFile) return@withContext false
        val digest = MessageDigest.getInstance("SHA-256")
        file.inputStream().use { input ->
            val buffer = ByteArray(64 * 1024)
            while (true) {
                val n = input.read(buffer)
                if (n == -1) break
                digest.update(buffer, 0, n)
            }
        }
        val actual = digest.digest().joinToString("") { "%02x".format(it) }
        actual.equals(expectedSha256.trim(), ignoreCase = true)
    }

    /**
     * Full install flow: download → verify → attest.
     *
     *  1. `api.download(slug, modelsDir/filename, onProgress)`.
     *  2. [verify] the bytes against `bundle.sha256` (no-op when sha is unknown).
     *     On mismatch the bad file is deleted and the call fails — so a corrupt
     *     download never lingers to be picked up by [installedModels].
     *  3. Write the `<slug>.json` sidecar (the model is now on disk + verified).
     *  4. `api.attest(...)` records it with the hub.
     *
     * **Attest-failure policy: keep the file.** A rejected attestation (e.g. a
     * transient backend error) leaves the verified bytes + sidecar in place so a
     * later retry can re-attest without re-downloading multiple GB. The call
     * still returns [Result.failure]; [installedModels] will list the bundle
     * because the verified bytes are present.
     *
     * @param version attest version string. Uses [bundle.slug]'s implicit "1.0"
     *   bundle version — there is no per-bundle version field in the catalog yet,
     *   so a stable default is used and documented.
     *
     * **Concurrency precondition:** callers MUST NOT invoke [install] for the same
     * [bundle.slug] concurrently — it shares the `.part`/dest/sidecar paths.
     * Serialising same-slug installs is the caller's (ViewModel's) responsibility.
     */
    override suspend fun install(
        bundle: LocalBundle,
        operator: String,
        delegate: String,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<InstalledModel> = withContext(Dispatchers.IO) {
        modelsDir.mkdirs()
        val destFile = File(modelsDir, bundle.filename)

        val downloaded = api.download(bundle.slug, destFile, onProgress)
        if (downloaded.isFailure) {
            return@withContext Result.failure(
                downloaded.exceptionOrNull()
                    ?: IOException("download failed for ${bundle.slug}")
            )
        }
        val file = downloaded.getOrThrow()

        if (!verify(file, bundle.sha256)) {
            file.delete()
            sidecarFor(bundle.slug).delete()
            return@withContext Result.failure(
                IOException("checksum mismatch for ${bundle.slug} (expected ${bundle.sha256})")
            )
        }

        // Bytes are on disk + verified → record the sidecar so installedModels
        // sees it even if attest later fails (kept-for-retry policy).
        val config = bundle.toModelConfig()
        writeSidecar(bundle, file.length(), config)

        // W2 review Minor 1: carry the SAME config we just wrote to the sidecar
        // onto the returned model, so install()'s result matches what a later
        // installedModels() scan reports (no silent all-defaults divergence).
        val installed = InstalledModel(
            slug = bundle.slug,
            file = file,
            sizeBytes = file.length(),
            config = config,
        )

        // Attest-throw safety: the sidecar is already written, so ANY attest
        // outcome other than success — `false` OR a thrown exception — must
        // become Result.failure with the verified file + sidecar KEPT (the
        // documented re-attest retry policy), never an uncaught throw.
        val attested = try {
            api.attest(
                AttestRequest(
                    operator = operator,
                    deviceId = deviceId,
                    modelSlug = bundle.slug,
                    version = BUNDLE_VERSION,
                    sha256 = bundle.sha256 ?: "",
                    delegate = delegate,
                    // autonomyMode left at its "permission" default.
                )
            )
        } catch (e: Exception) {
            // Keep the verified file + sidecar for a re-attest retry.
            return@withContext Result.failure(e)
        }
        if (!attested) {
            // Keep the verified file + sidecar for a re-attest retry.
            return@withContext Result.failure(IOException("attestation rejected for ${bundle.slug}"))
        }

        Result.success(installed)
    }

    /**
     * Remove the model file for [slug] (and its sidecar). Returns whether
     * anything was actually deleted. Disk I/O runs on [Dispatchers.IO].
     */
    override suspend fun delete(slug: String): Boolean = withContext(Dispatchers.IO) {
        val sidecar = sidecarFor(slug)
        // Resolve the bundle file from the sidecar if present; otherwise nothing
        // to do (we only know filenames via sidecars).
        val record = runCatching {
            if (sidecar.isFile) json.decodeFromString(SidecarRecord.serializer(), sidecar.readText())
            else null
        }.getOrNull()

        var removed = false
        if (record != null) {
            val file = File(modelsDir, record.filename)
            if (file.exists() && file.delete()) removed = true
        }
        if (sidecar.exists() && sidecar.delete()) removed = true
        removed
    }

    private fun sidecarFor(slug: String) = File(modelsDir, slug + SIDECAR_SUFFIX)

    /**
     * Derive the per-model [ModelConfig] (Task W2) recorded in the sidecar from a
     * catalog [LocalBundle]. The catalog (GET /local/models/catalog) now advertises
     * the per-model config fields (Task W6), so this maps the REAL values through
     * to the `<slug>.json` sidecar (it previously yielded all-defaults). A legacy
     * catalog response without these fields still maps cleanly — they default to
     * null/false on [LocalBundle], producing a legacy-equivalent [ModelConfig].
     */
    private fun LocalBundle.toModelConfig(): ModelConfig = ModelConfig(
        maxTokens = maxTokens,
        supportImage = supportImage,
        recommended = recommended,
        contextNote = contextNote,
        topK = topK,
        topP = topP,
        temperature = temperature,
    )

    private fun writeSidecar(bundle: LocalBundle, sizeBytes: Long, config: ModelConfig) {
        val record = SidecarRecord(
            slug = bundle.slug,
            filename = bundle.filename,
            sizeBytes = sizeBytes,
            maxTokens = config.maxTokens,
            supportImage = config.supportImage,
            recommended = config.recommended,
            contextNote = config.contextNote,
            topK = config.topK,
            topP = config.topP,
            temperature = config.temperature,
        )
        sidecarFor(bundle.slug).writeText(json.encodeToString(SidecarRecord.serializer(), record))
    }

    /** Small on-disk record that lets [installedModels] map files → slugs. */
    @Serializable
    private data class SidecarRecord(
        val slug: String,
        val filename: String,
        @SerialName("size_bytes") val sizeBytes: Long,
        // Per-model config (Task W2). OPTIONAL + defaulted so LEGACY sidecars
        // (only slug/filename/size_bytes) still deserialize unchanged.
        @SerialName("max_tokens") val maxTokens: Int? = null,
        @SerialName("support_image") val supportImage: Boolean = false,
        @SerialName("recommended") val recommended: Boolean = false,
        @SerialName("context_note") val contextNote: String? = null,
        @SerialName("top_k") val topK: Int? = null,
        @SerialName("top_p") val topP: Float? = null,
        @SerialName("temperature") val temperature: Float? = null,
    ) {
        fun toModelConfig(): ModelConfig = ModelConfig(
            maxTokens = maxTokens,
            supportImage = supportImage,
            recommended = recommended,
            contextNote = contextNote,
            topK = topK,
            topP = topP,
            temperature = temperature,
        )
    }

    companion object {
        /** 1 GiB, matching ActivityManager.MemoryInfo.totalMem's byte units. */
        const val BYTES_PER_GIB: Long = 1_073_741_824L

        /** Suffix for the per-model install record written next to each bundle. */
        const val SIDECAR_SUFFIX = ".json"

        /**
         * Attest version. The catalog has no per-bundle version field yet, so a
         * stable "1.0" is used; bump when the bundle format/version is tracked.
         */
        const val BUNDLE_VERSION = "1.0"

        // ignoreUnknownKeys: legacy/future sidecars stay forward-compatible.
        // encodeDefaults: the WRITER emits the W2 per-model keys even at their
        // defaults, so a written sidecar is self-documenting + forward-stable
        // (round-trips through SidecarRecord -> installedModels unchanged).
        private val json = Json {
            ignoreUnknownKeys = true
            encodeDefaults = true
        }

        /** Subdir of `context.filesDir` where bundles + sidecars are stored. */
        private const val MODELS_SUBDIR = "local_models"

        /**
         * Production wiring: read real device RAM (ActivityManager
         * MemoryInfo.totalMem) and use `filesDir/local_models` for storage.
         * Thin glue — all Android-framework access lives here, not in the
         * testable core, so this factory needs no unit test.
         */
        @JvmStatic
        fun fromContext(
            context: android.content.Context,
            api: LocalModelDownloader,
            deviceId: String,
        ): LocalModelManager {
            val modelsDir = File(context.filesDir, MODELS_SUBDIR)
            val ram: () -> Long = {
                val am = context.getSystemService(android.content.Context.ACTIVITY_SERVICE)
                        as android.app.ActivityManager
                val info = android.app.ActivityManager.MemoryInfo()
                am.getMemoryInfo(info)
                info.totalMem
            }
            return LocalModelManager(
                api = api,
                modelsDir = modelsDir,
                totalRamBytes = ram,
                deviceId = deviceId,
            )
        }
    }
}
