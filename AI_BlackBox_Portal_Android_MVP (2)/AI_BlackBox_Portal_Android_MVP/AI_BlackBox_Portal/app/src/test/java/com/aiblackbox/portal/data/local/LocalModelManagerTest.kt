package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.LocalModelDownloader
import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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

    private val e2b = LocalBundle(
        slug = "gemma-4-e2b",
        displayName = "Gemma 4 E2B (on-device)",
        hfRepo = "litert-community/gemma-4-e2b-it-litert-lm",
        filename = "gemma-4-e2b-it.litertlm",
        minRamGb = 3.0,
        recommendedFor = "Lighter, faster on-device model.",
    )
    private val e4b = LocalBundle(
        slug = "gemma-4-e4b",
        displayName = "Gemma 4 E4B (on-device)",
        hfRepo = "litert-community/gemma-4-e4b-it-litert-lm",
        filename = "gemma-4-e4b-it.litertlm",
        minRamGb = 6.0,
        recommendedFor = "Higher quality for high-RAM phones.",
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
    fun `recommendForDevice picks heaviest that fits on a high-RAM phone`() = runTest {
        val mgr = manager(FakeDownloader(ByteArray(0)), ramGb = 8.0)
        val pick = mgr.recommendForDevice(listOf(e2b, e4b))
        assertEquals("8GB phone gets E4B (min 6.0)", "gemma-4-e4b", pick.slug)
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
        val bundle = e2b.copy(sha256 = sha256Hex(content))
        val mgr = manager(FakeDownloader(content), ramGb = 4.0)

        assertTrue(mgr.install(bundle, operator = "Brandon", delegate = "cpu") { _, _ -> }.isSuccess)

        // The written sidecar must emit the W2 keys (today at their defaults, since
        // the catalog bundle carries no per-model config yet) so the format is
        // forward-stable and re-parses to the same ModelConfig.
        val sidecarText = modelsDir.resolve("${bundle.slug}.json").readText()
        assertTrue("sidecar emits max_tokens", sidecarText.contains("max_tokens"))
        assertTrue("sidecar emits support_image", sidecarText.contains("support_image"))
        assertTrue("sidecar emits top_k", sidecarText.contains("top_k"))

        // Round-trip: re-reading yields a defaulted ModelConfig (maxTokens null).
        val cfg = mgr.installedModels().single().config
        assertNull(cfg.maxTokens)
        assertFalse(cfg.supportImage)
    }
}
