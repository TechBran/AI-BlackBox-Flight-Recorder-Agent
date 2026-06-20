package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import java.security.MessageDigest
import kotlin.io.path.createTempDirectory

/**
 * Unit tests for [LocalModelManager] — the orchestration layer above
 * [com.aiblackbox.portal.data.api.LocalModelApi]. Fully hermetic plain-JUnit:
 * a [FakeDownloader] stands in for the network API (no MockWebServer, no
 * Android Context — RAM and deviceId are constructor seams), so these run on
 * the JVM with zero framework dependencies.
 *
 * Coverage:
 *   1. recommendForDevice — E4B on a high-RAM phone, E2B on a low-RAM phone,
 *      the lightest when RAM is below every threshold.
 *   2. verify — true on sha match, false on mismatch, true when expected is
 *      null/blank (no-op when the catalog sha is unknown pre-fetch).
 *   3. install happy path — download + verify + attest all succeed → success.
 *   4. install verify-failure — wrong sha → failure AND the bad file is deleted.
 *   5. install attest-failure — attest false → failure, file KEPT for retry.
 *   6. installedModels — reflects on-disk state across install + delete.
 */
class LocalModelManagerTest {

    private lateinit var modelsDir: File

    /** Captures the manager's warn-log lines so tests assert WITHOUT android.util.Log. */
    private val logLines = mutableListOf<String>()

    // Fixtures carry the Task W6 per-model config the backend catalog advertises:
    // E4B is the recommended default; E2B is the experimental fallback.
    private val e2b = LocalBundle(
        slug = "gemma-4-e2b",
        displayName = "Gemma 4 E2B (on-device)",
        hfRepo = "litert-community/gemma-4-e2b-it-litert-lm",
        filename = "gemma-4-e2b-it.litertlm",
        minRamGb = 3.0,
        recommendedFor = "Lighter, faster on-device model.",
        recommended = false,
        contextNote = "Experimental — weaker at multi-step agent loops",
        maxTokens = 16384,
        supportImage = true,
    )
    private val e4b = LocalBundle(
        slug = "gemma-4-e4b",
        displayName = "Gemma 4 E4B (on-device)",
        hfRepo = "litert-community/gemma-4-e4b-it-litert-lm",
        filename = "gemma-4-e4b-it.litertlm",
        minRamGb = 6.0,
        recommendedFor = "Higher quality for high-RAM phones.",
        recommended = true,
        contextNote = "Recommended — best on-device agent reliability",
        maxTokens = 16384,
        supportImage = true,
    )

    @Before fun setUp() {
        modelsDir = createTempDirectory(prefix = "localmodelmanager-test").toFile()
    }

    @After fun tearDown() {
        modelsDir.deleteRecursively()
    }

    private fun sha256Hex(bytes: ByteArray): String =
        MessageDigest.getInstance("SHA-256").digest(bytes)
            .joinToString("") { "%02x".format(it) }

    private fun manager(
        api: LocalModelDownloader,
        ramGb: Double,
        deviceId: String = "test-device",
    ) = LocalModelManager(
        api = api,
        modelsDir = modelsDir,
        totalRamBytes = { (ramGb * 1_073_741_824L).toLong() },
        deviceId = deviceId,
        // Capture instead of calling android.util.Log (un-mocked on the JVM gate).
        logWarn = { _, message -> logLines.add(message) },
    )

    /**
     * Fake downloader: writes [content] bytes to the destFile (mimicking the
     * real resumable download's final-file handoff) and returns a configurable
     * attest result. Records the last AttestRequest for assertions.
     */
    private class FakeDownloader(
        private val content: ByteArray,
        private val attestResult: Boolean = true,
        private val downloadOk: Boolean = true,
    ) : LocalModelDownloader {
        var lastAttest: AttestRequest? = null
        var downloadCalledFor: String? = null

        override suspend fun download(
            slug: String,
            destFile: File,
            onProgress: (Long, Long) -> Unit,
        ): Result<File> {
            downloadCalledFor = slug
            if (!downloadOk) return Result.failure(java.io.IOException("download failed"))
            destFile.parentFile?.mkdirs()
            onProgress(0L, content.size.toLong())
            destFile.writeBytes(content)
            onProgress(content.size.toLong(), content.size.toLong())
            return Result.success(destFile)
        }

        override suspend fun attest(req: AttestRequest): Boolean {
            lastAttest = req
            return attestResult
        }
    }

    // -------------------------------------------------------------------------
    // 1. recommendForDevice
    // -------------------------------------------------------------------------

    @Test
    fun `recommendForDevice prefers the recommended default (E4B) on a high-RAM phone`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        // W6: E4B is the catalog-recommended default and it fits → it wins, and
        // it carries the "Recommended…" note.
        assertEquals("8GB phone gets the recommended E4B", "gemma-4-e4b", pick.slug)
        assertTrue("E4B is the recommended default", pick.recommended)
        assertTrue("E4B surfaces the Recommended note", pick.contextNote!!.contains("Recommended"))
    }

    @Test
    fun `recommendForDevice prefers E4B even when listed first (flag, not order)`() = runTest {
        // E2B listed FIRST: the choice is driven by the recommended flag + RAM,
        // not list order — E4B (recommended, fits at 8GB) must still win.
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        assertEquals("gemma-4-e4b", pick.slug)
    }

    @Test
    fun `recommendForDevice falls back to E2B on low RAM and surfaces its experimental note`() = runTest {
        // 4GB fits E2B (3.0) but not the recommended E4B (6.0): the only fitting
        // bundle is E2B, and the caller must surface ITS "Experimental…" note.
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 4.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        assertEquals("gemma-4-e2b", pick.slug)
        assertFalse("E2B is not the recommended default", pick.recommended)
        assertTrue("low-RAM fallback surfaces the Experimental note",
            pick.contextNote!!.contains("Experimental"))
    }

    @Test
    fun `recommendForDevice picks lighter that fits on a low-RAM phone`() = runTest {
        // 4GB fits E2B (3.0) but not E4B (6.0).
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 4.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        assertEquals("4GB phone gets E2B", "gemma-4-e2b", pick.slug)
    }

    @Test
    fun `recommendForDevice returns lightest when none fit`() = runTest {
        // 2GB fits neither (E2B needs 3.0) → fall back to the lightest bundle.
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 2.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        assertEquals("2GB phone gets the lightest (E2B)", "gemma-4-e2b", pick.slug)
    }

    // -------------------------------------------------------------------------
    // 2. verify
    // -------------------------------------------------------------------------

    @Test
    fun `verify returns true on matching sha256`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val bytes = ByteArray(1024) { (it % 251).toByte() }
        val f = File(modelsDir, "match.bin").apply { writeBytes(bytes) }
        assertTrue(mgr.verify(f, sha256Hex(bytes)))
    }

    @Test
    fun `verify returns false on mismatched sha256`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val f = File(modelsDir, "mismatch.bin").apply { writeBytes(ByteArray(512) { 1 }) }
        assertFalse(mgr.verify(f, "deadbeef"))
    }

    @Test
    fun `verify is a no-op (true) when expected sha is null or blank`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val f = File(modelsDir, "unknown.bin").apply { writeBytes(ByteArray(256) { 2 }) }
        assertTrue("null sha → can't verify → accept", mgr.verify(f, null))
        assertTrue("blank sha → can't verify → accept", mgr.verify(f, "   "))
    }

    // -------------------------------------------------------------------------
    // 3. install — happy path
    // -------------------------------------------------------------------------

    @Test
    fun `install succeeds when download, verify and attest all pass`() = runTest {
        val content = ByteArray(4096) { (it % 251).toByte() }
        val bundle = e4b.copy(sha256 = sha256Hex(content), sizeBytes = content.size.toLong())
        val fake = FakeDownloader(content, attestResult = true)
        val mgr = manager(fake, ramGb = 8.0, deviceId = "pixel-9")

        val result = mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }

        assertTrue("install should succeed: ${result.exceptionOrNull()}", result.isSuccess)
        val installed = result.getOrThrow()
        assertEquals("gemma-4-e4b", installed.slug)
        assertEquals(content.size.toLong(), installed.sizeBytes)
        assertTrue("model file must exist", installed.file.exists())
        assertEquals(File(modelsDir, bundle.filename), installed.file)

        // attest carried the right operator/device/slug/sha/delegate.
        val req = fake.lastAttest!!
        assertEquals("Brandon", req.operator)
        assertEquals("pixel-9", req.deviceId)
        assertEquals("gemma-4-e4b", req.modelSlug)
        assertEquals(sha256Hex(content), req.sha256)
        assertEquals("gpu", req.delegate)
        assertEquals("permission", req.autonomyMode)

        // installedModels now sees it.
        assertEquals(1, mgr.installedModels().size)
        assertEquals("gemma-4-e4b", mgr.installedModels().first().slug)
    }

    @Test
    fun `install succeeds with a null catalog sha (verify is skipped)`() = runTest {
        val content = ByteArray(1000) { 7 }
        val bundle = e2b.copy(sha256 = null)
        val fake = FakeDownloader(content, attestResult = true)
        val mgr = manager(fake, ramGb = 4.0)

        val result = mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }

        assertTrue("install should succeed with null sha", result.isSuccess)
        assertTrue(File(modelsDir, bundle.filename).exists())
        // attest still sent, with empty-string sha (backend default).
        assertEquals("", fake.lastAttest!!.sha256)
    }

    // -------------------------------------------------------------------------
    // 4. install — verify failure deletes the bad file
    // -------------------------------------------------------------------------

    @Test
    fun `install fails and deletes the file when checksum does not match`() = runTest {
        val content = ByteArray(2048) { (it % 251).toByte() }
        val bundle = e4b.copy(sha256 = "0000000000000000000000000000000000000000000000000000000000000000")
        val fake = FakeDownloader(content, attestResult = true)
        val mgr = manager(fake, ramGb = 8.0)

        val result = mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }

        assertTrue("install must fail on checksum mismatch", result.isFailure)
        val f = File(modelsDir, bundle.filename)
        assertFalse("corrupt file must be deleted", f.exists())
        // And it must NOT show up as installed.
        assertTrue("installedModels must be empty after a failed verify", mgr.installedModels().isEmpty())
        // Verify failure short-circuits before attest.
        assertNull("attest must not be called on verify failure", fake.lastAttest)
    }

    // -------------------------------------------------------------------------
    // 5. install — attest failure keeps the file for retry
    // -------------------------------------------------------------------------

    @Test
    fun `install fails but keeps the verified file when attest is rejected`() = runTest {
        val content = ByteArray(2048) { (it % 251).toByte() }
        val bundle = e4b.copy(sha256 = sha256Hex(content))
        val fake = FakeDownloader(content, attestResult = false)
        val mgr = manager(fake, ramGb = 8.0)

        val result = mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }

        assertTrue("install must fail when attest is rejected", result.isFailure)
        val f = File(modelsDir, bundle.filename)
        assertTrue("verified file is KEPT for attest retry", f.exists())
        // Because the bytes are on disk + verified, installedModels lists it.
        assertEquals(1, mgr.installedModels().size)
    }

    // -------------------------------------------------------------------------
    // 6. installedModels — reflects on-disk state
    // -------------------------------------------------------------------------

    @Test
    fun `installedModels reflects install then delete`() = runTest {
        val content = ByteArray(1024) { 9 }
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val fake = FakeDownloader(content)
        val mgr = manager(fake, ramGb = 4.0)

        assertTrue("nothing installed initially", mgr.installedModels().isEmpty())

        mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }
        val listed = mgr.installedModels()
        assertEquals(1, listed.size)
        assertEquals("gemma-4-e2b", listed.first().slug)

        val deleted = mgr.delete("gemma-4-e2b")
        assertTrue("delete reports it removed something", deleted)
        assertTrue("gone after delete", mgr.installedModels().isEmpty())

        // Deleting again removes nothing.
        assertFalse("second delete removes nothing", mgr.delete("gemma-4-e2b"))
    }

    @Test
    fun `installedModels skips corrupt sidecar`() = runTest {
        val content = ByteArray(1024) { 5 }
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 4.0)

        // One valid install...
        mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }
        // ...plus a garbage .json sidecar that must be skipped, not throw.
        modelsDir.resolve("garbage.json").writeText("{not valid")

        val listed = mgr.installedModels()
        assertEquals("only the valid entry is returned", 1, listed.size)
        assertEquals("gemma-4-e2b", listed.first().slug)
    }

    @Test
    fun `installedModels returns the good model when ANOTHER sidecar is malformed (R2)`() = runTest {
        // R2 core fix: the device-runtime "no models available" failure mode was a
        // single bad sidecar emptying the whole list. A present, parseable model
        // (bundle on disk) must ALWAYS list even alongside a malformed sidecar.
        val content = ByteArray(1024) { 6 }
        val bundle = e4b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 8.0)

        // One GOOD install (sidecar + bundle present)...
        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }.isSuccess)
        // ...plus a MALFORMED sidecar that must NOT abort the scan.
        modelsDir.resolve("broken.json").writeText("}{ this is not json")

        val listed = mgr.installedModels()
        assertEquals("the good model must still be listed", 1, listed.size)
        assertEquals("gemma-4-e4b", listed.single().slug)
    }

    @Test
    fun `installedModels logs the skipped sidecar (filename + class, no content) and a summary (R2)`() = runTest {
        val content = ByteArray(1024) { 7 }
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 4.0)
        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }.isSuccess)
        modelsDir.resolve("broken.json").writeText("not-json-at-all")

        logLines.clear()
        val listed = mgr.installedModels()
        assertEquals(1, listed.size)

        // A per-skip warn naming the FILE (not the content) + a one-line summary.
        val skipLine = logLines.firstOrNull { it.contains("broken.json") }
        assertNotNull("a skip line names the bad sidecar file", skipLine)
        assertFalse("the log must NOT echo sidecar content", logLines.any { it.contains("not-json-at-all") })
        assertTrue("summary line present", logLines.any { it.contains("found 1") && it.contains("skipped 1") })
    }

    @Test
    fun `installedModels parses a legacy-minimal sidecar slug filename size_bytes only (R2 regression)`() = runTest {
        // Regression: the smallest legacy sidecar shape must still parse + list.
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        writeSidecarJson(
            slug = "gemma-legacy",
            json = """
                {
                  "slug": "gemma-legacy",
                  "filename": "gemma-legacy.litertlm",
                  "size_bytes": 8
                }
            """.trimIndent(),
        )
        val model = mgr.installedModels().single()
        assertEquals("gemma-legacy", model.slug)
        assertEquals(8L, model.sizeBytes)
        assertNull("legacy sidecar carries no max_tokens", model.config.maxTokens)
    }

    // -------------------------------------------------------------------------
    // 7. Per-model config sidecar (Task W2)
    // -------------------------------------------------------------------------

    /** Write a sidecar file directly (bypasses install) for parse-shape tests. */
    private fun writeSidecarJson(slug: String, json: String) {
        modelsDir.mkdirs()
        modelsDir.resolve("$slug.json").writeText(json)
        // installedModels() only returns sidecars whose bundle file exists.
        modelsDir.resolve("$slug.litertlm").writeBytes(ByteArray(8) { 1 })
    }

    @Test
    fun `installedModels parses a sidecar WITH the new per-model config fields`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        writeSidecarJson(
            slug = "gemma-4-e4b",
            json = """
                {
                  "slug": "gemma-4-e4b",
                  "filename": "gemma-4-e4b.litertlm",
                  "size_bytes": 8,
                  "max_tokens": 8192,
                  "support_image": true,
                  "recommended": true,
                  "context_note": "Higher quality; 8GB+ phones.",
                  "top_k": 40,
                  "top_p": 0.9,
                  "temperature": 0.7
                }
            """.trimIndent(),
        )

        val cfg = mgr.installedModels().single().config
        assertEquals(8192, cfg.maxTokens)
        assertTrue("support_image -> supportImage true", cfg.supportImage)
        assertTrue("recommended true", cfg.recommended)
        assertEquals("Higher quality; 8GB+ phones.", cfg.contextNote)
        assertEquals(40, cfg.topK)
        assertEquals(0.9f, cfg.topP)
        assertEquals(0.7f, cfg.temperature)
    }

    @Test
    fun `installedModels parses a LEGACY sidecar (no config fields) with defaults`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        // A pre-W2 sidecar: ONLY slug/filename/size_bytes. Must still parse.
        writeSidecarJson(
            slug = "gemma-4-e2b",
            json = """
                {
                  "slug": "gemma-4-e2b",
                  "filename": "gemma-4-e2b.litertlm",
                  "size_bytes": 8
                }
            """.trimIndent(),
        )

        val model = mgr.installedModels().single()
        assertEquals("gemma-4-e2b", model.slug)
        val cfg = model.config
        // maxTokens null -> caller falls back to the engine default.
        assertNull("legacy sidecar has no max_tokens", cfg.maxTokens)
        assertFalse("supportImage defaults false", cfg.supportImage)
        assertFalse("recommended defaults false", cfg.recommended)
        assertNull(cfg.contextNote)
        assertNull(cfg.topK)
        assertNull(cfg.topP)
        assertNull(cfg.temperature)
    }

    @Test
    fun `installedModels tolerates unknown future sidecar keys`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        writeSidecarJson(
            slug = "gemma-4-e2b",
            json = """
                {
                  "slug": "gemma-4-e2b",
                  "filename": "gemma-4-e2b.litertlm",
                  "size_bytes": 8,
                  "max_tokens": 4096,
                  "some_future_key": "ignored"
                }
            """.trimIndent(),
        )
        val cfg = mgr.installedModels().single().config
        assertEquals(4096, cfg.maxTokens)
    }

    @Test
    fun `install writes a sidecar carrying the new config keys (round-trip)`() = runTest {
        val content = ByteArray(1024) { 3 }
        // E2B fixture now carries the real W6 config (max_tokens 16384, multimodal).
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 4.0)

        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }.isSuccess)

        // The written sidecar must emit the W2 keys so the format is forward-stable
        // and re-parses to the same ModelConfig.
        val sidecarText = modelsDir.resolve("${bundle.slug}.json").readText()
        assertTrue("sidecar emits max_tokens", sidecarText.contains("max_tokens"))
        assertTrue("sidecar emits support_image", sidecarText.contains("support_image"))
        assertTrue("sidecar emits top_k", sidecarText.contains("top_k"))

        // Round-trip: re-reading yields the bundle's REAL config (W6), not defaults.
        val cfg = mgr.installedModels().single().config
        assertEquals(16384, cfg.maxTokens)
        assertTrue(cfg.supportImage)
        assertFalse("E2B is not recommended", cfg.recommended)
        assertTrue("E2B carries the experimental note",
            cfg.contextNote!!.contains("Experimental"))
    }

    // -------------------------------------------------------------------------
    // 8. Per-model catalog config → ModelConfig mapping (Task W6)
    // -------------------------------------------------------------------------

    @Test
    fun `install maps the E4B catalog config into the written sidecar (recommended + note)`() = runTest {
        val content = ByteArray(2048) { 4 }
        val bundle = e4b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 8.0)

        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }.isSuccess)

        // Re-read via installedModels → the sidecar carried E4B's real config.
        val cfg = mgr.installedModels().single().config
        assertEquals(16384, cfg.maxTokens)
        assertTrue("E4B is multimodal", cfg.supportImage)
        assertTrue("E4B is the recommended default", cfg.recommended)
        assertTrue("E4B carries the Recommended note",
            cfg.contextNote!!.contains("Recommended"))
    }

    @Test
    fun `install returns an InstalledModel whose config equals the written sidecar (Minor 1)`() = runTest {
        // W2 review Minor 1: install()'s returned InstalledModel.config must match
        // the config persisted in the sidecar (no all-defaults divergence).
        val content = ByteArray(2048) { 5 }
        val bundle = e4b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 8.0)

        val returned = mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }
            .getOrThrow()
        // What install() returned...
        val returnedCfg = returned.config
        // ...must equal what a fresh installedModels() scan reads from the sidecar.
        val sidecarCfg = mgr.installedModels().single().config

        assertEquals("returned config == sidecar config", sidecarCfg, returnedCfg)
        // And it is the bundle's real (non-default) config, not ModelConfig().
        assertEquals(16384, returnedCfg.maxTokens)
        assertTrue(returnedCfg.recommended)
        assertTrue(returnedCfg.supportImage)
        assertEquals("Recommended — best on-device agent reliability", returnedCfg.contextNote)
    }

    // -------------------------------------------------------------------------
    // 9. mergedConfig — pure settings merge (on-device settings apply layer)
    // -------------------------------------------------------------------------

    @Test
    fun `mergedConfig overwrites maxTokens and the sampler trio, preserving other fields`() {
        val old = ModelConfig(
            maxTokens = 4096,
            supportImage = true,
            recommended = true,
            contextNote = "keep me",
            topK = 40,
            topP = 0.9f,
            temperature = 0.7f,
        )
        val merged = mergedConfig(
            old,
            maxTokens = 8192,
            sampler = SamplerSettings(topK = 64, topP = 0.95f, temperature = 1.0f),
        )
        assertEquals("maxTokens overwritten", 8192, merged.maxTokens)
        assertEquals("topK overwritten", 64, merged.topK)
        assertEquals("topP overwritten", 0.95f, merged.topP)
        assertEquals("temperature overwritten", 1.0f, merged.temperature)
        // Untouched, non-settings fields must survive verbatim.
        assertTrue("supportImage preserved", merged.supportImage)
        assertTrue("recommended preserved", merged.recommended)
        assertEquals("contextNote preserved", "keep me", merged.contextNote)
    }

    @Test
    fun `mergedConfig leaves existing values when all args are null`() {
        val old = ModelConfig(
            maxTokens = 4096,
            supportImage = true,
            topK = 40,
            topP = 0.9f,
            temperature = 0.7f,
        )
        val merged = mergedConfig(old, maxTokens = null, sampler = null)
        assertEquals("no-arg call leaves config unchanged", old, merged)
    }

    @Test
    fun `mergedConfig changes only maxTokens when sampler is null`() {
        val old = ModelConfig(maxTokens = 4096, topK = 40, topP = 0.9f, temperature = 0.7f)
        val merged = mergedConfig(old, maxTokens = 2048, sampler = null)
        assertEquals(2048, merged.maxTokens)
        // Null sampler leaves the whole trio intact.
        assertEquals(40, merged.topK)
        assertEquals(0.9f, merged.topP)
        assertEquals(0.7f, merged.temperature)
    }

    @Test
    fun `mergedConfig preserves a sampler axis left null inside a provided sampler`() {
        // A provided sampler with one null axis is a FIELD-WISE merge, not a reset:
        // the null axis keeps the old value; the set axes overwrite.
        val old = ModelConfig(maxTokens = 4096, topK = 40, topP = 0.9f, temperature = 0.7f)
        val merged = mergedConfig(
            old,
            maxTokens = null,
            sampler = SamplerSettings(topK = 64, topP = null, temperature = null),
        )
        assertEquals("maxTokens kept (null arg)", 4096, merged.maxTokens)
        assertEquals("topK overwritten", 64, merged.topK)
        assertEquals("topP preserved (null axis)", 0.9f, merged.topP)
        assertEquals("temperature preserved (null axis)", 0.7f, merged.temperature)
    }

    @Test
    fun `mergedConfig can set values on an all-default ModelConfig`() {
        val merged = mergedConfig(
            ModelConfig(),
            maxTokens = 16384,
            sampler = SamplerSettings(topK = 32, topP = 0.8f, temperature = 0.6f),
        )
        assertEquals(16384, merged.maxTokens)
        assertEquals(32, merged.topK)
        assertEquals(0.8f, merged.topP)
        assertEquals(0.6f, merged.temperature)
        // Defaults for untouched fields stay default.
        assertFalse(merged.supportImage)
        assertFalse(merged.recommended)
        assertNull(merged.contextNote)
    }

    // -------------------------------------------------------------------------
    // 10. updateModelConfig — persist a user settings change (round-trip)
    // -------------------------------------------------------------------------

    @Test
    fun `updateModelConfig rewrites the sidecar and installedModels reads the new config`() = runTest {
        val content = ByteArray(2048) { 4 }
        val bundle = e4b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 8.0)
        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "gpu") { _, _ -> }.isSuccess)

        val ok = mgr.updateModelConfig(
            "gemma-4-e4b",
            maxTokens = 8192,
            sampler = SamplerSettings(topK = 16, topP = 0.85f, temperature = 0.5f),
        )
        assertTrue("update reports success", ok)

        // installedModels re-reads the SAME serializer/path -> sees the new config.
        val cfg = mgr.installedModels().single().config
        assertEquals(8192, cfg.maxTokens)
        assertEquals(16, cfg.topK)
        assertEquals(0.85f, cfg.topP)
        assertEquals(0.5f, cfg.temperature)
        // Non-settings fields the install wrote must survive the rewrite.
        assertTrue("supportImage preserved through rewrite", cfg.supportImage)
        assertTrue("recommended preserved through rewrite", cfg.recommended)
        assertTrue("contextNote preserved", cfg.contextNote!!.contains("Recommended"))
        // The model is still listed (filename/size carried through).
        assertEquals("gemma-4-e4b", mgr.installedModels().single().slug)
    }

    @Test
    fun `updateModelConfig with null args leaves the existing config unchanged`() = runTest {
        val content = ByteArray(1024) { 3 }
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 4.0)
        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }.isSuccess)
        val before = mgr.installedModels().single().config

        assertTrue(mgr.updateModelConfig("gemma-4-e2b", maxTokens = null, sampler = null))

        assertEquals("null-arg update is a no-change rewrite", before, mgr.installedModels().single().config)
    }

    @Test
    fun `updateModelConfig returns false for an uninstalled slug (no sidecar)`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val ok = mgr.updateModelConfig(
            "not-installed",
            maxTokens = 8192,
            sampler = SamplerSettings(topK = 16, topP = 0.85f, temperature = 0.5f),
        )
        assertFalse("no sidecar -> nothing persisted", ok)
        assertTrue(mgr.installedModels().isEmpty())
    }
}
