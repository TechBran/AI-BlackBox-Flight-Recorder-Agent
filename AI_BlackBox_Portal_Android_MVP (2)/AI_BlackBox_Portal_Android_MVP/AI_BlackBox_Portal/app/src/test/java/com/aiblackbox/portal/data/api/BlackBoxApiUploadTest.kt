package com.aiblackbox.portal.data.api

import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Before
import org.junit.Test
import java.io.File

/**
 * Terminal File Attach plan, Task 15 — BlackBoxApi.uploadFile multi-field
 * multipart + FastAPI error `detail` surfacing.
 *
 * The upcoming CliAttachButton posts to `/cli-agent/zellij/attach-file` with a
 * `session_name` text field alongside the file part, and must show the
 * backend's `{"detail": "..."}` message on failure instead of a bare
 * "HTTP <code>". These tests pin:
 *   (a) extra `fields` become form-data parts BEFORE the file part;
 *   (b) non-2xx responses surface FastAPI `detail` (stringified when it is
 *       not a string, e.g. 422 validation-error arrays) via [ApiHttpException];
 *   (c) the default (no `fields`) wire format is unchanged — a single file
 *       part — so existing /upload and /stt call sites are unaffected.
 */
class BlackBoxApiUploadTest {

    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi
    private lateinit var file: File

    @Before fun setUp() {
        server = MockWebServer()
        server.start()
        api = BlackBoxApi(server.url("").toString().trimEnd('/'))
        file = File.createTempFile("attach-me", ".txt").apply { writeText("file-payload-bytes") }
    }

    @After fun tearDown() {
        server.close()
        file.delete()
    }

    private fun ok() = MockResponse.Builder().code(200).body("{\"ok\":true}").build()

    // -------------------------------------------------------------------------
    // (a) extra fields present, before the file part
    // -------------------------------------------------------------------------

    @Test
    fun `extra form fields are sent as parts before the file part`() = runTest {
        server.enqueue(ok())

        api.uploadFile(
            "/cli-agent/zellij/attach-file?op=brandon",
            file,
            fields = mapOf("session_name" to "cc-main", "purpose" to "terminal-attach"),
        )

        val body = server.takeRequest().body!!.utf8()
        // Both text fields present, with their values.
        assertTrue(body.contains("name=\"session_name\""))
        assertTrue(body.contains("cc-main"))
        assertTrue(body.contains("name=\"purpose\""))
        assertTrue(body.contains("terminal-attach"))
        // The file part keeps its field name, filename, and content.
        assertTrue(body.contains("name=\"file\"; filename=\"${file.name}\""))
        assertTrue(body.contains("file-payload-bytes"))
        // Fields come BEFORE the file part, so stream-parsing servers see the
        // metadata before the (potentially large) payload.
        assertTrue(body.indexOf("name=\"session_name\"") < body.indexOf("filename=\""))
        assertTrue(body.indexOf("name=\"purpose\"") < body.indexOf("filename=\""))
    }

    // -------------------------------------------------------------------------
    // (c) default fields — wire format unchanged
    // -------------------------------------------------------------------------

    @Test
    fun `default fields adds no extra parts - single file part only`() = runTest {
        server.enqueue(ok())

        api.uploadFile("/upload", file)

        val body = server.takeRequest().body!!.utf8()
        assertEquals(1, Regex("Content-Disposition: form-data").findAll(body).count())
        assertTrue(body.contains("name=\"file\"; filename=\"${file.name}\""))
    }

    // -------------------------------------------------------------------------
    // (b) FastAPI detail extraction on non-2xx
    // -------------------------------------------------------------------------

    @Test
    fun `non-2xx surfaces FastAPI detail string`() = runTest {
        server.enqueue(MockResponse.Builder().code(409).body("{\"detail\":\"boom\"}").build())

        try {
            api.uploadFile("/cli-agent/zellij/attach-file", file, fields = mapOf("session_name" to "x"))
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertEquals("boom", e.message)
        }
    }

    @Test
    fun `non-string detail is stringified - 422 validation array`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(422)
                .body("{\"detail\":[{\"loc\":[\"body\",\"file\"],\"msg\":\"field required\"}]}")
                .build()
        )

        try {
            api.uploadFile("/upload", file)
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertTrue(
                "expected stringified detail, got: ${e.message}",
                e.message!!.contains("field required")
            )
        }
    }

    @Test
    fun `non-JSON error body falls back to HTTP status line`() = runTest {
        server.enqueue(MockResponse.Builder().code(500).body("<html>oops</html>").build())

        try {
            api.uploadFile("/upload", file)
            fail("expected ApiHttpException")
        } catch (e: ApiHttpException) {
            assertTrue("expected HTTP fallback, got: ${e.message}", e.message!!.startsWith("HTTP 500"))
        }
    }
}
