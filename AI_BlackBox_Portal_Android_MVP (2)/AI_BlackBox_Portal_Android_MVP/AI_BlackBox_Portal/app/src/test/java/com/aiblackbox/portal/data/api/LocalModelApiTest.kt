package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.AttestRequest
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import okio.Buffer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.File
import kotlin.io.path.createTempDirectory

/**
 * Unit tests for [LocalModelApi] — the Android client for the hub's `local`
 * (on-device Gemma) endpoints. Hermetic: a real OkHttp call path is exercised
 * against MockWebServer (mockwebserver3, already a testImplementation dep), so
 * the actual BlackBoxApi base-URL + OkHttpClient + kotlinx.serialization parse
 * paths run — matching TtsVoiceParseTest's convention.
 *
 * Coverage:
 *   1. catalog()  — parses {"bundles":[...]} into List<LocalBundle>.
 *   2. download()  — streams served bytes to destFile + reports progress.
 *   3. download() RESUME — pre-existing .part triggers Range: bytes=N-, the
 *      206 remainder appends, final file == full content.
 *   4. attest()  — posts the correct JSON body, returns true on success.
 *   5. status()  — parses {"available", "models"} for both available states.
 */
class LocalModelApiTest {

    private lateinit var server: MockWebServer
    private lateinit var api: LocalModelApi
    private lateinit var tmpDir: File

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash so that the
        // path concatenation ("$baseUrl$path") preserves the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        api = LocalModelApi(BlackBoxApi(baseUrl))
        tmpDir = createTempDirectory(prefix = "localmodelapi-test").toFile()
    }

    @After fun tearDown() {
        server.close()
        tmpDir.deleteRecursively()
    }

    private fun enqueueJson(body: String, code: Int = 200) {
        server.enqueue(
            MockResponse.Builder()
                .code(code)
                .headers(headersOf("Content-Type", "application/json"))
                .body(body)
                .build()
        )
    }

    // -------------------------------------------------------------------------
    // 1. catalog()
    // -------------------------------------------------------------------------

    @Test
    fun `catalog parses bundles into LocalBundle list`() = runTest {
        // size_bytes + sha256 null on one bundle (placeholder pre-fetch), filled
        // on the other — both must parse. min_ram_gb is a float.
        enqueueJson(
            """
            {"bundles":[
              {"slug":"gemma-4-e2b","display_name":"Gemma 4 E2B (on-device)",
               "hf_repo":"litert-community/gemma-4-e2b-it-litert-lm",
               "filename":"gemma-4-e2b-it.litertlm",
               "size_bytes":null,"sha256":null,"min_ram_gb":3.0,
               "recommended_for":"Lighter, faster on-device model."},
              {"slug":"gemma-4-e4b","display_name":"Gemma 4 E4B (on-device)",
               "hf_repo":"litert-community/gemma-4-e4b-it-litert-lm",
               "filename":"gemma-4-e4b-it.litertlm",
               "size_bytes":4294967296,"sha256":"abc123","min_ram_gb":6.0,
               "recommended_for":"Higher quality for high-RAM phones.",
               "download_url":"https://huggingface.co/litert-community/gemma-4-e4b-it-litert-lm/resolve/main/gemma-4-e4b-it.litertlm",
               "gated":true}
            ]}
            """.trimIndent()
        )

        val bundles = api.catalog()

        assertEquals(2, bundles.size)

        val e2b = bundles[0]
        assertEquals("gemma-4-e2b", e2b.slug)
        assertEquals("Gemma 4 E2B (on-device)", e2b.displayName)
        assertEquals("litert-community/gemma-4-e2b-it-litert-lm", e2b.hfRepo)
        assertEquals("gemma-4-e2b-it.litertlm", e2b.filename)
        assertNull("size_bytes null pre-fetch", e2b.sizeBytes)
        assertNull("sha256 null pre-fetch", e2b.sha256)
        assertEquals(3.0, e2b.minRamGb, 0.0)
        assertEquals("Lighter, faster on-device model.", e2b.recommendedFor)

        val e4b = bundles[1]
        assertEquals("gemma-4-e4b", e4b.slug)
        assertEquals(4294967296L, e4b.sizeBytes)  // > Int.MAX_VALUE → must be Long
        assertEquals("abc123", e4b.sha256)
        assertEquals(6.0, e4b.minRamGb, 0.0)
        // Direct-from-HF fields (2026-06-27): download_url + gated parse.
        assertEquals(
            "https://huggingface.co/litert-community/gemma-4-e4b-it-litert-lm/resolve/main/gemma-4-e4b-it.litertlm",
            e4b.downloadUrl,
        )
        assertTrue("gated must parse true", e4b.gated)
        // E2B omits both — defaults apply (empty downloadUrl, gated false).
        assertEquals("", e2b.downloadUrl)
        assertFalse("gated defaults false when absent", e2b.gated)

        // Hit the real GET /local/models/catalog endpoint.
        assertEquals("/local/models/catalog", server.takeRequest().target)
    }

    // -------------------------------------------------------------------------
    // 2. download() — fresh download writes bytes + reports progress.
    // -------------------------------------------------------------------------

    @Test
    fun `download writes served bytes to destFile and reports progress`() = runTest {
        val content = ByteArray(2048) { (it % 251).toByte() }
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/octet-stream"))
                .body(Buffer().write(content))
                .build()
        )

        val dest = File(tmpDir, "gemma.litertlm")
        var lastSoFar = 0L
        var lastTotal = -1L
        var sawProgress = false

        val result = api.download("gemma-4-e2b", dest) { soFar, total ->
            sawProgress = true
            lastSoFar = soFar
            lastTotal = total
        }

        assertTrue("download should succeed: ${result.exceptionOrNull()}", result.isSuccess)
        assertEquals(dest, result.getOrNull())
        assertTrue("destFile must exist", dest.exists())
        assertArrayEquals(content, dest.readBytes())

        assertTrue("onProgress must be invoked", sawProgress)
        assertEquals("final bytesSoFar == bytes written", content.size.toLong(), lastSoFar)
        assertEquals("total reported from Content-Length", content.size.toLong(), lastTotal)

        // No leftover .part temp file once the rename succeeded.
        assertFalse(".part temp must be gone after success", File(tmpDir, "gemma.litertlm.part").exists())

        // Hit the real GET /local/models/download/{slug} endpoint, no Range on a
        // fresh download.
        val recorded = server.takeRequest()
        assertEquals("/local/models/download/gemma-4-e2b", recorded.target)
        assertNull("fresh download must NOT send a Range header", recorded.headers["Range"])
    }

    // -------------------------------------------------------------------------
    // 3. download() RESUME — pre-existing .part → Range: bytes=N- → append.
    // -------------------------------------------------------------------------

    @Test
    fun `download resumes from a partial part file via a Range request`() = runTest {
        val full = ByteArray(4096) { (it % 251).toByte() }
        val prefixLen = 1500
        val prefix = full.copyOfRange(0, prefixLen)
        val remainder = full.copyOfRange(prefixLen, full.size)

        // Pre-create the .part with the first K bytes (a prior interrupted run).
        val dest = File(tmpDir, "gemma-resume.litertlm")
        val part = File(tmpDir, "gemma-resume.litertlm.part")
        part.writeBytes(prefix)

        // Server answers the resumed range with a 206 + Content-Range carrying the
        // total, and only the remaining bytes in the body.
        server.enqueue(
            MockResponse.Builder()
                .code(206)
                .headers(
                    headersOf(
                        "Content-Type", "application/octet-stream",
                        "Content-Range", "bytes $prefixLen-${full.size - 1}/${full.size}",
                        "Accept-Ranges", "bytes",
                    )
                )
                .body(Buffer().write(remainder))
                .build()
        )

        var lastSoFar = 0L
        var lastTotal = -1L
        val result = api.download("gemma-4-e4b", dest) { soFar, total ->
            lastSoFar = soFar
            lastTotal = total
        }

        assertTrue("resume download should succeed: ${result.exceptionOrNull()}", result.isSuccess)
        assertArrayEquals("resumed file must equal full content", full, dest.readBytes())

        // Progress counts the already-present prefix toward both ends.
        assertEquals(full.size.toLong(), lastSoFar)
        assertEquals(full.size.toLong(), lastTotal)

        // The request MUST carry an open-ended Range header from the existing
        // byte count.
        val recorded = server.takeRequest()
        assertEquals("/local/models/download/gemma-4-e4b", recorded.target)
        assertEquals("bytes=$prefixLen-", recorded.headers["Range"])

        assertFalse(".part must be renamed away on success", part.exists())
    }

    // -------------------------------------------------------------------------
    // 4. attest()
    // -------------------------------------------------------------------------

    @Test
    fun `attest posts the correct JSON body and returns true on success`() = runTest {
        enqueueJson(
            """
            {"success":true,"device":{"device_id":"pixel-9","model_slug":"gemma-4-e4b",
             "version":"1.0","sha256":"abc","delegate":"gpu",
             "autonomy_mode":"permission","verified_at":1.0}}
            """.trimIndent()
        )

        val ok = api.attest(
            AttestRequest(
                operator = "Brandon",
                deviceId = "pixel-9",
                modelSlug = "gemma-4-e4b",
                version = "1.0",
                sha256 = "abc",
                delegate = "gpu",
            )
        )
        assertTrue("attest returns true on {success:true}", ok)

        val recorded = server.takeRequest()
        assertEquals("/local/device/attest", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        // Body must carry the backend's snake_case field names + the values.
        assertTrue("operator in body", sentBody.contains("\"operator\":\"Brandon\""))
        assertTrue("device_id in body", sentBody.contains("\"device_id\":\"pixel-9\""))
        assertTrue("model_slug in body", sentBody.contains("\"model_slug\":\"gemma-4-e4b\""))
        assertTrue("autonomy_mode default in body", sentBody.contains("\"autonomy_mode\":\"permission\""))
    }

    @Test
    fun `attest returns false when backend reports success false`() = runTest {
        enqueueJson("""{"success":false,"error":"operator required"}""", code = 400)
        val ok = api.attest(AttestRequest(operator = "x", deviceId = "y"))
        assertFalse("attest must be false on a non-success response", ok)
    }

    // -------------------------------------------------------------------------
    // 5. status()
    // -------------------------------------------------------------------------

    @Test
    fun `status parses available true with a model record`() = runTest {
        enqueueJson(
            """
            {"available":true,"models":[
              {"device_id":"pixel-9","model_slug":"gemma-4-e4b","version":"1.0",
               "sha256":"abc","delegate":"gpu","autonomy_mode":"yolo",
               "verified_at":1718000000.0}
            ]}
            """.trimIndent()
        )

        val status = api.status("Brandon")
        assertTrue(status.available)
        assertEquals(1, status.models.size)
        val rec = status.models[0]
        assertEquals("pixel-9", rec.deviceId)
        assertEquals("gemma-4-e4b", rec.modelSlug)
        assertEquals("yolo", rec.autonomyMode)

        // operator must be passed as a query param.
        val recorded = server.takeRequest()
        assertTrue(
            "status must query ?operator=Brandon; got '${recorded.target}'",
            recorded.target.startsWith("/local/device/status") &&
                recorded.target.contains("operator=Brandon"),
        )
    }

    @Test
    fun `status parses available false with empty models`() = runTest {
        enqueueJson("""{"available":false,"models":[]}""")
        val status = api.status("Nobody")
        assertFalse(status.available)
        assertTrue(status.models.isEmpty())
    }

    // -------------------------------------------------------------------------
    // 6. setAutonomy() — POST /local/device/autonomy {operator, device_id, mode}
    // -------------------------------------------------------------------------

    @Test
    fun `setAutonomy posts operator device_id and mode and returns true on success`() = runTest {
        enqueueJson("""{"success":true}""")

        val ok = api.setAutonomy(operator = "Brandon", deviceId = "pixel-9", mode = "yolo")
        assertTrue("setAutonomy returns true on {success:true}", ok)

        val recorded = server.takeRequest()
        assertEquals("/local/device/autonomy", recorded.target)
        assertEquals("POST", recorded.method)
        val sentBody = recorded.body!!.utf8()
        assertTrue("operator in body", sentBody.contains("\"operator\":\"Brandon\""))
        assertTrue("device_id (snake_case) in body", sentBody.contains("\"device_id\":\"pixel-9\""))
        assertTrue("mode in body", sentBody.contains("\"mode\":\"yolo\""))
    }

    @Test
    fun `setAutonomy returns false when backend rejects`() = runTest {
        enqueueJson("""{"success":false,"error":"operator required"}""", code = 400)
        val ok = api.setAutonomy(operator = "x", deviceId = "y", mode = "permission")
        assertFalse("setAutonomy must be false on a non-success response", ok)
    }

    // -------------------------------------------------------------------------
    // 7. systemPrompt() — GET /local/system-prompt?operator=… → {prompt, version}
    // -------------------------------------------------------------------------

    @Test
    fun `systemPrompt GETs operator query and parses prompt and version`() = runTest {
        enqueueJson(
            """{"prompt":"You are BlackBox. Be direct, no sycophancy.","version":"a1b2c3d"}"""
        )

        // An operator with a space + ampersand exercises URL-encoding.
        val persona = api.systemPrompt("Brandon DEV & Co")

        assertEquals("You are BlackBox. Be direct, no sycophancy.", persona.prompt)
        assertEquals("a1b2c3d", persona.version)

        val recorded = server.takeRequest()
        assertTrue(
            "must hit /local/system-prompt; got '${recorded.target}'",
            recorded.target.startsWith("/local/system-prompt"),
        )
        // operator passed + URL-encoded (space → %20, & → %26).
        assertTrue(
            "operator must be URL-encoded in the query; got '${recorded.target}'",
            recorded.target.contains("operator=Brandon%20DEV%20%26%20Co"),
        )
    }

    // assertArrayEquals helper (JUnit's is on org.junit.Assert; import via FQN to
    // keep the byte-array overload explicit).
    private fun assertArrayEquals(expected: ByteArray, actual: ByteArray) =
        org.junit.Assert.assertArrayEquals(expected, actual)

    private fun assertArrayEquals(msg: String, expected: ByteArray, actual: ByteArray) =
        org.junit.Assert.assertArrayEquals(msg, expected, actual)
}
