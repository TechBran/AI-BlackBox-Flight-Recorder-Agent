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
 * One on-device Gemma model that is present on disk.
 *
 * Returned by [LocalModelManager.installedModels] and [LocalModelManager.install].
 * `file` is the actual bundle file under the manager's `modelsDir`.
 */
data class InstalledModel(
    val slug: String,
    val file: File,
    val sizeBytes: Long,
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
) {

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
    suspend fun installedModels(): List<InstalledModel> = withContext(Dispatchers.IO) {
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
            InstalledModel(slug = record.slug, file = file, sizeBytes = file.length())
        }.sortedBy { it.slug }
    }

    /**
     * Pick the bundle best matched to this device's RAM: the heaviest bundle
     * whose `minRamGb` fits in [totalRamBytes] (so a high-RAM phone gets E4B, a
     * low-RAM phone gets E2B). If none fit, fall back to the lightest bundle.
     *
     * @throws IllegalArgumentException if [bundles] is empty.
     */
    suspend fun recommendForDevice(bundles: List<LocalBundle>): LocalBundle {
        require(bundles.isNotEmpty()) { "recommendForDevice requires a non-empty bundle list" }
        val ram = totalRamBytes()
        val fitting = bundles.filter { (it.minRamGb * BYTES_PER_GIB).toLong() <= ram }
        return if (fitting.isNotEmpty()) {
            fitting.maxByOrNull { it.minRamGb }!!
        } else {
            bundles.minByOrNull { it.minRamGb }!!
        }
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
    suspend fun install(
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
        writeSidecar(bundle, file.length())

        val installed = InstalledModel(slug = bundle.slug, file = file, sizeBytes = file.length())

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
    suspend fun delete(slug: String): Boolean = withContext(Dispatchers.IO) {
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

    private fun writeSidecar(bundle: LocalBundle, sizeBytes: Long) {
        val record = SidecarRecord(
            slug = bundle.slug,
            filename = bundle.filename,
            sizeBytes = sizeBytes,
        )
        sidecarFor(bundle.slug).writeText(json.encodeToString(SidecarRecord.serializer(), record))
    }

    /** Small on-disk record that lets [installedModels] map files → slugs. */
    @Serializable
    private data class SidecarRecord(
        val slug: String,
        val filename: String,
        @SerialName("size_bytes") val sizeBytes: Long,
    )

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

        private val json = Json { ignoreUnknownKeys = true }

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
