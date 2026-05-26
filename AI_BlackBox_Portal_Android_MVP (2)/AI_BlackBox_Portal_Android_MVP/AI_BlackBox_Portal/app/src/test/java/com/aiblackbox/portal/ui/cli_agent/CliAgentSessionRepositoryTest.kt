package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.api.BlackBoxApi
import kotlinx.coroutines.test.runTest
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.io.IOException

/**
 * T19 unit tests for [CliAgentSessionRepository]'s new Zellij endpoints.
 *
 * Uses [MockWebServer] (mockwebserver3) to stand in for the orchestrator
 * at runtime, so the tests exercise the actual OkHttp client wired into
 * [BlackBoxApi] — URL paths, query strings, request bodies, and response
 * parsing all go through the real wire format.
 *
 * Convention: the repository methods THROW on failure (matching the rest
 * of the repository layer). Tests use try/catch + fail-style assertions
 * via `assertThrows`-equivalent patterns since the codebase is on JUnit 4.
 *
 * Coverage:
 *   - launchZellijSession: POST URL + body shape + parses into [ZellijSession].
 *   - launchZellijSession with optional `app`: serialized into request body.
 *   - listZellijSessions: GET URL + parses into [ZellijSessionRow] list.
 *   - killZellijSession: DELETE URL with operator query string.
 *   - getZellijBackendStatus: parses the field set we care about.
 *   - HTTP 403 (operator-prefix gate) → IOException.
 *   - HTTP 500 / transport drop → IOException.
 *   - Unknown provider slug rejected client-side via IllegalArgumentException.
 *   - 'agy' alias rejected (we send long-form 'antigravity').
 */
class CliAgentSessionRepositoryTest {

    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi
    private lateinit var repo: CliAgentSessionRepository

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        // BlackBoxApi expects a baseUrl WITHOUT a trailing slash so that
        // path concatenation (`"$baseUrl$path"`) keeps the leading slash.
        val baseUrl = server.url("").toString().trimEnd('/')
        api = BlackBoxApi(baseUrl)
        repo = CliAgentSessionRepository(api)
    }

    @After
    fun tearDown() {
        server.close()
    }

    // --- launchZellijSession ---------------------------------------------

    @Test
    fun `launchZellijSession POSTs to launch endpoint with op query and parses response`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """
                    {
                      "session_name": "Brandon__claude___root__1779750372",
                      "session_url": "http://blackbox.local:9097/?session=Brandon__claude___root__1779750372#token-abc",
                      "token": "token-abc",
                      "expires_at": "2026-05-25T12:00:00+00:00"
                    }
                    """.trimIndent()
                )
                .build()
        )

        val session = repo.launchZellijSession("Brandon", "claude")

        assertEquals("Brandon__claude___root__1779750372", session.name)
        assertEquals("claude", session.provider)
        assertTrue(session.sessionUrl.startsWith("http://blackbox.local:9097/"))
        assertEquals("token-abc", session.token)
        assertEquals("2026-05-25T12:00:00+00:00", session.expiresAt)

        val request = server.takeRequest()
        assertEquals("POST", request.method)
        assertEquals("/cli-agent/zellij/launch?op=Brandon", request.target)
        val sentJson = Json.parseToJsonElement(request.body!!.utf8()).jsonObject
        assertEquals("claude", sentJson["provider"]?.jsonPrimitive?.content)
        // app omitted entirely when null — don't send `"app": null` over the wire
        assertFalse(
            "app key should be omitted when null, got: ${request.body!!.utf8()}",
            sentJson.containsKey("app"),
        )
    }

    @Test
    fun `launchZellijSession includes app field in body when non-null`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """
                    {
                      "session_name": "Brandon__terminal__myapp__1",
                      "session_url": "http://blackbox.local:9097/?session=x#t",
                      "token": "t",
                      "expires_at": null
                    }
                    """.trimIndent()
                )
                .build()
        )

        val session = repo.launchZellijSession("Brandon", "terminal", app = "myapp")
        assertEquals("myapp", session.app)
        assertNull("terminal provider has no TTL → expires_at null", session.expiresAt)

        val request = server.takeRequest()
        val sentJson = Json.parseToJsonElement(request.body!!.utf8()).jsonObject
        assertEquals("terminal", sentJson["provider"]?.jsonPrimitive?.content)
        assertEquals("myapp", sentJson["app"]?.jsonPrimitive?.content)
    }

    @Test
    fun `launchZellijSession URL-encodes operator names with reserved chars`() = runTest {
        // "Brandon DEV" exercises the encoder for real — space → "+".
        // (Hyphens are unreserved and pass through; this case used to assert
        // a no-op, swap to a name that actually demands encoding.)
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """{"session_name":"x","session_url":"x","token":"t","expires_at":null}"""
                )
                .build()
        )
        repo.launchZellijSession("Brandon DEV", "gemini")

        val request = server.takeRequest()
        assertEquals("/cli-agent/zellij/launch?op=Brandon+DEV", request.target)
    }

    @Test
    fun `launchZellijSession rejects unknown provider client-side without HTTP call`() = runTest {
        var thrown: Throwable? = null
        try {
            repo.launchZellijSession("Brandon", "not-a-real-provider")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("unknown provider must throw", thrown)
        assertTrue(
            "expected IllegalArgumentException, got ${thrown!!::class.simpleName}",
            thrown is IllegalArgumentException,
        )
        assertEquals(
            "No HTTP request should be made for invalid provider",
            0,
            server.requestCount,
        )
    }

    @Test
    fun `launchZellijSession rejects 'agy' alias (we send long-form 'antigravity')`() = runTest {
        // The backend accepts both 'agy' and 'antigravity'; the Android
        // client deliberately allows only the long form so we have ONE
        // canonical slug flowing through telemetry + UI.
        var thrown: Throwable? = null
        try {
            repo.launchZellijSession("Brandon", "agy")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("agy alias must throw", thrown)
        assertTrue(thrown is IllegalArgumentException)
        assertEquals(0, server.requestCount)
    }

    // --- listZellijSessions ----------------------------------------------

    @Test
    fun `listZellijSessions GETs sessions endpoint with op query and parses array`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """
                    {
                      "sessions": [
                        {"name":"Brandon__claude___root__1","provider":"claude","app":null,"created_at":"2026-05-25T10:00:00Z","expires_at":"2026-05-25T10:05:00Z"},
                        {"name":"Brandon__terminal__myapp__2","provider":"terminal","app":"myapp","created_at":"2026-05-25T11:00:00Z","expires_at":null}
                      ]
                    }
                    """.trimIndent()
                )
                .build()
        )

        val sessions = repo.listZellijSessions("Brandon")
        assertEquals(2, sessions.size)
        assertEquals("Brandon__claude___root__1", sessions[0].name)
        assertEquals("claude", sessions[0].provider)
        assertNull(sessions[0].app)
        assertEquals("2026-05-25T10:00:00Z", sessions[0].createdAt)
        assertEquals("2026-05-25T10:05:00Z", sessions[0].expiresAt)
        assertEquals("Brandon__terminal__myapp__2", sessions[1].name)
        assertEquals("myapp", sessions[1].app)
        assertNull("terminal provider has no TTL", sessions[1].expiresAt)

        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/cli-agent/zellij/sessions?op=Brandon", request.target)
    }

    @Test
    fun `listZellijSessions returns empty list when sessions array is empty`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[]}""")
                .build()
        )
        val sessions = repo.listZellijSessions("Brandon")
        assertTrue("empty list is a clean success", sessions.isEmpty())
    }

    // --- killZellijSession -----------------------------------------------

    @Test
    fun `killZellijSession DELETEs the right URL with operator query param`() = runTest {
        // 204 No Content — empty body.
        server.enqueue(MockResponse.Builder().code(204).build())

        val sessionName = "Brandon__claude___root__1779750372"
        repo.killZellijSession("Brandon", sessionName)

        val request = server.takeRequest()
        assertEquals("DELETE", request.method)
        assertEquals(
            "/cli-agent/zellij/sessions/$sessionName?op=Brandon",
            request.target,
        )
    }

    @Test
    fun `killZellijSession throws IOException on HTTP 403 (operator-prefix gate)`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(403)
                .body("""{"detail":"Cannot delete session belonging to another operator"}""")
                .build()
        )
        var thrown: Throwable? = null
        try {
            repo.killZellijSession("Brandon", "Mallory__claude___root__9999")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("403 must surface as a throw", thrown)
        assertTrue("expected IOException, got ${thrown!!::class.simpleName}", thrown is IOException)
        assertTrue(
            "Underlying error should mention HTTP 403; got '${thrown.message}'",
            thrown.message?.contains("403") == true,
        )
    }

    // --- getZellijBackendStatus ------------------------------------------

    @Test
    fun `getZellijBackendStatus parses fields and hits backend-status endpoint`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """
                    {
                      "web_daemon_running": true,
                      "session_count_total": 7,
                      "my_session_count": 2,
                      "configured_backend": "zellij",
                      "effective_backend": "zellij"
                    }
                    """.trimIndent()
                )
                .build()
        )
        val status = repo.getZellijBackendStatus("Brandon")
        assertTrue(status.webDaemonRunning)
        assertEquals(7, status.sessionCountTotal)
        assertEquals(2, status.mySessionCount)
        assertEquals("zellij", status.configuredBackend)
        assertEquals("zellij", status.effectiveBackend)

        val request = server.takeRequest()
        assertEquals("GET", request.method)
        assertEquals("/cli-agent/zellij/backend-status?op=Brandon", request.target)
    }

    @Test
    fun `getZellijBackendStatus parses three-field minimum (configured and effective optional)`() = runTest {
        // Older deployments returning only the brief's spec'd 3 fields must
        // still parse — extras are tolerated by Json{ignoreUnknownKeys=true}.
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """{"web_daemon_running":false,"session_count_total":0,"my_session_count":0}"""
                )
                .build()
        )
        val status = repo.getZellijBackendStatus("Brandon")
        assertFalse(status.webDaemonRunning)
        assertEquals(0, status.sessionCountTotal)
        assertEquals(0, status.mySessionCount)
        assertNull(status.configuredBackend)
        assertNull(status.effectiveBackend)
    }

    // --- Error mapping ---------------------------------------------------

    @Test
    fun `launchZellijSession throws on HTTP 500`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(500)
                .body("""{"detail":"boom"}""")
                .build()
        )
        var thrown: Throwable? = null
        try {
            repo.launchZellijSession("Brandon", "claude")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("HTTP 500 must throw", thrown)
        assertTrue(thrown is IOException)
    }

    @Test
    fun `listZellijSessions throws on network drop (no response enqueued)`() = runTest {
        // Shut the server BEFORE the call so OkHttp gets a ConnectException.
        server.close()
        var thrown: Throwable? = null
        try {
            repo.listZellijSessions("Brandon")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("network drop must throw", thrown)
        // OkHttp wraps connect failures as IOException subclasses.
        assertTrue(
            "expected IOException, got ${thrown!!::class.simpleName}",
            thrown is IOException,
        )
    }

    @Test
    fun `launchZellijSession throws on malformed JSON`() = runTest {
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("not valid json at all")
                .build()
        )
        var thrown: Throwable? = null
        try {
            repo.launchZellijSession("Brandon", "claude")
        } catch (t: Throwable) {
            thrown = t
        }
        assertNotNull("malformed JSON must throw", thrown)
    }

    // --- Smoke: domain type integrity ------------------------------------

    @Test
    fun `ZellijListResponse can round-trip an empty payload`() {
        // Defensive: bare {"sessions":[]} parses to empty list (default).
        val parsed = Json { ignoreUnknownKeys = true }
            .decodeFromString(
                com.aiblackbox.portal.data.model.ZellijListResponse.serializer(),
                """{"sessions":[]}"""
            )
        assertTrue(parsed.sessions.isEmpty())
    }
}
