package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.ZellijWebSocketClient
import com.aiblackbox.portal.data.model.ZellijSession
import com.aiblackbox.portal.data.model.ZellijSessionRow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.job
import kotlinx.coroutines.joinAll
import kotlinx.coroutines.test.runTest
import mockwebserver3.MockResponse
import mockwebserver3.MockWebServer
import okhttp3.Headers.Companion.headersOf
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test

/**
 * T21 unit tests for [CliAgentScreenState] — the screen-scoped state
 * holder that owns sessions/launchInFlight/currentSession for both the
 * empty state ([CliAgentEmptyState]) and the session switcher top bar
 * ([SessionSwitcherTopBar]).
 *
 * Uses MockWebServer + a real [CliAgentSessionRepository] so the test
 * exercises the full wire format (matches the [CliAgentSessionRepositoryTest]
 * pattern — same wire fixture, one level up the stack).
 *
 * **Why `runTest { ... }` + `joinChildren()`:**
 * the holder spawns background coroutines via `scope.launch { ... }` to
 * drive repo calls without blocking the UI. To assert the post-state
 * deterministically we have to wait for those children to complete. We
 * inject `this` (the `runTest` `TestScope`) as the holder's scope so all
 * child coroutines become children of the test scope; then [joinChildren]
 * awaits them via [kotlinx.coroutines.Job.children] + [joinAll] — a public
 * `kotlinx.coroutines` API path.
 *
 * (`advanceUntilIdle()` from `kotlinx-coroutines-test` would be more
 * idiomatic in a pure-virtual-time test, but it only drains coroutines on
 * the `TestDispatcher`; the repo's HTTP work runs on OkHttp's real IO
 * threads, so virtual-time advancement won't await it. `joinChildren()`
 * uses [Job.join] which suspends until the actual Job completes regardless
 * of dispatcher, which is what this hybrid TestScope + real-IO setup needs.)
 *
 * Coverage per T21 brief:
 *   - launchInFlight add + remove on success (try/finally invariant).
 *   - launchInFlight remove on failure (repo throws IOException).
 *   - sessions list refresh after launch (synthetic row + reconcile).
 *   - currentSession is set on launch and cleared on kill.
 *   - duplicate launch for an in-flight provider is a no-op.
 */
@OptIn(ExperimentalCoroutinesApi::class)
class CliAgentScreenStateTest {

    private lateinit var server: MockWebServer
    private lateinit var api: BlackBoxApi
    private lateinit var repo: CliAgentSessionRepository

    @Before
    fun setUp() {
        server = MockWebServer()
        server.start()
        val baseUrl = server.url("").toString().trimEnd('/')
        api = BlackBoxApi(baseUrl)
        repo = CliAgentSessionRepository(api)
        // The holder's kill() delegates to the process-lived
        // TerminalSessionManager singleton; reset it so state can't leak
        // across tests.
        TerminalSessionManager.resetForTest()
    }

    @After
    fun tearDown() {
        server.close()
        TerminalSessionManager.resetForTest()
    }

    /**
     * Wait for all child coroutines of [this] scope to complete, including
     * any children spawned transitively while we're already awaiting. Loops
     * until no children remain — necessary because [CliAgentScreenState.launch]
     * spawns a follow-up [CliAgentScreenState.refreshSessions] child from
     * inside its own coroutine body, which only appears in the parent's
     * children list AFTER the launch child started.
     *
     * Uses the public [kotlinx.coroutines.Job.children] + [Job.join] API
     * path. Suspends on the actual Jobs, which is required because the
     * repo's HTTP work runs on OkHttp's real IO threads —
     * `advanceUntilIdle()` would not await them.
     */
    private suspend fun CoroutineScope.joinChildren() {
        do {
            val pending = coroutineContext.job.children.toList()
            pending.joinAll()
        } while (coroutineContext.job.children.any { it.isActive })
    }

    /** Build a holder wired to [scope]; errors collected for inspection. */
    private fun newHolder(
        scope: CoroutineScope,
        onLaunchedSessionNames: MutableList<String> = mutableListOf(),
        errors: MutableList<Pair<String, String>> = mutableListOf(),
    ): CliAgentScreenState = CliAgentScreenState(
        scope = scope,
        repository = repo,
        operator = "Brandon",
        onLaunched = { onLaunchedSessionNames.add(it.name) },
        onError = { action, reason -> errors.add(action to reason) },
    )

    // ── launchInFlight ───────────────────────────────────────────────────

    @Test
    fun `launch adds provider to launchInFlight then removes on success`() = runTest {
        val onLaunched = mutableListOf<String>()
        val holder = newHolder(this, onLaunched)

        // Launch response.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"Brandon__claude___root__1","session_url":"http://x","token":"t","expires_at":null}""")
                .build()
        )
        // Follow-up refresh inside launch() — returns the new row.
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"Brandon__claude___root__1","provider":"claude"}]}""")
                .build()
        )

        holder.launch("claude")
        assertTrue(
            "launchInFlight should contain 'claude' immediately after launch fires",
            "claude" in holder.launchInFlight,
        )

        joinChildren()

        assertTrue(
            "launchInFlight should be cleared after successful launch",
            "claude" !in holder.launchInFlight,
        )
        assertEquals(1, holder.sessions.size)
        assertEquals("Brandon__claude___root__1", holder.sessions[0].name)
        assertEquals(listOf("Brandon__claude___root__1"), onLaunched)
        assertNotNull(holder.currentSession)
        assertEquals("Brandon__claude___root__1", holder.currentSession?.name)
    }

    @Test
    fun `launch removes provider from launchInFlight when repo throws`() = runTest {
        val errors = mutableListOf<Pair<String, String>>()
        val holder = newHolder(this, errors = errors)

        // 500 → IOException out of BlackBoxApi.
        server.enqueue(
            MockResponse.Builder()
                .code(500)
                .body("""{"detail":"boom"}""")
                .build()
        )

        holder.launch("gemini")
        joinChildren()

        assertTrue(
            "Failed launch must still clear launchInFlight (finally block)",
            "gemini" !in holder.launchInFlight,
        )
        assertTrue("error callback should fire", errors.isNotEmpty())
        assertEquals("launch", errors[0].first)
        assertTrue(holder.sessions.isEmpty())
        assertNull(holder.currentSession)
    }

    @Test
    fun `launch is a no-op when same provider is already in flight`() = runTest {
        val holder = newHolder(this)

        // Only enqueue ONE launch response — if the second call were
        // dispatched, MockWebServer would block waiting for a second
        // enqueue and the test would hang. The implementation must early-out.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"a","session_url":"x","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"a","provider":"claude"}]}""")
                .build()
        )

        holder.launch("claude")
        // Immediate second call before the first completes — should be ignored.
        holder.launch("claude")
        joinChildren()

        // Only the first request (launch) + follow-up refresh (2 total)
        // should have been issued, not 4.
        assertEquals(
            "Duplicate launch must not issue a second HTTP launch request",
            2,
            server.requestCount,
        )
        assertEquals(1, holder.sessions.size)
    }

    // ── fresh-by-default + YOLO (2026-07-03) ─────────────────────────────

    @Test
    fun `launch always sends fork=true in the request body`() = runTest {
        val holder = newHolder(this)

        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"Brandon__claude___root__1__99","session_url":"x","token":"t","expires_at":null,"resumed":false}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"Brandon__claude___root__1__99","provider":"claude"}]}""")
                .build()
        )

        // Plain default launch — no fork parameter exists any more; every
        // tap is a fresh spawn.
        holder.launch("claude")
        joinChildren()

        val request = server.takeRequest()
        val sentJson = kotlinx.serialization.json.Json
            .parseToJsonElement(request.body!!.utf8())
            .let { it as kotlinx.serialization.json.JsonObject }
        assertEquals(
            "every launch must send fork=true (fresh-by-default)",
            "true",
            sentJson["fork"]?.let { (it as kotlinx.serialization.json.JsonPrimitive).content },
        )
    }

    @Test
    fun `launch with yolo=true sends yolo in the request body`() = runTest {
        val holder = newHolder(this)

        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"Brandon__claude___root_yolo__7","session_url":"x","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"Brandon__claude___root_yolo__7","provider":"claude","yolo":true}]}""")
                .build()
        )

        holder.launch("claude", yolo = true)
        joinChildren()

        val request = server.takeRequest()
        val sentJson = kotlinx.serialization.json.Json
            .parseToJsonElement(request.body!!.utf8())
            .let { it as kotlinx.serialization.json.JsonObject }
        assertEquals(
            "yolo must be threaded into the launch body as boolean true",
            "true",
            sentJson["yolo"]?.let { (it as kotlinx.serialization.json.JsonPrimitive).content },
        )
        // Synthesised row seeds the yolo flag so the ⚡ badge shows before
        // the first refresh reconciles from the server list.
        assertTrue(
            "synthesised session row must carry yolo=true",
            holder.sessions.any { it.name == "Brandon__claude___root_yolo__7" && it.yolo },
        )
    }

    @Test
    fun `launch default omits yolo from the request body`() = runTest {
        val holder = newHolder(this)

        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"s","session_url":"x","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"s","provider":"claude"}]}""")
                .build()
        )

        holder.launch("claude")
        joinChildren()

        val request = server.takeRequest()
        val sentJson = kotlinx.serialization.json.Json
            .parseToJsonElement(request.body!!.utf8())
            .let { it as kotlinx.serialization.json.JsonObject }
        assertTrue(
            "default launch must NOT carry a yolo key (server treats missing as false)",
            "yolo" !in sentJson.keys,
        )
    }

    @Test
    fun `launch failing with 409 surfaces the server detail verbatim`() = runTest {
        val errors = mutableListOf<Pair<String, String>>()
        val holder = newHolder(this, errors = errors)

        val capDetail = "Session limit reached (12). Close a session (X) first."
        server.enqueue(
            MockResponse.Builder()
                .code(409)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"detail":"$capDetail"}""")
                .build()
        )

        holder.launch("claude")
        joinChildren()

        assertEquals(
            "409 cap detail must reach onError verbatim (BlackBoxApi.errorFor prefers detail)",
            listOf("launch" to capDetail),
            errors,
        )
        assertTrue("no session must be added on a capped launch", holder.sessions.isEmpty())
        assertTrue("spinner must clear on 409", "claude" !in holder.launchInFlight)
    }

    @Test
    fun `launch failing with a transport error surfaces a prefixed message`() = runTest {
        val errors = mutableListOf<Pair<String, String>>()
        val holder = newHolder(this, errors = errors)

        // Close the server so the connect attempt fails: a raw transport
        // error (ConnectException) with no HTTP `detail` — the opposite of
        // the 409 case above. Its bare message ("Failed to connect to …")
        // must NOT be shown verbatim; it should be prefixed with context.
        server.close()

        holder.launch("gemini")
        joinChildren()

        assertEquals("exactly one launch error expected", 1, errors.size)
        val (action, reason) = errors.first()
        assertEquals("launch", action)
        assertTrue(
            "transport failures must be prefixed with context, not shown raw (was: \"$reason\")",
            reason.startsWith("Couldn't launch gemini:"),
        )
        assertTrue("spinner must clear on a transport failure", "gemini" !in holder.launchInFlight)
    }

    // ── kill ──────────────────────────────────────────────────────────────

    @Test
    fun `kill removes session and clears currentSession when it was current`() = runTest {
        val holder = newHolder(this)

        // Seed: launch one session so we have something to kill.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"to-kill","session_url":"x","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"to-kill","provider":"claude"}]}""")
                .build()
        )
        holder.launch("claude")
        joinChildren()
        val row = holder.sessions.first()

        // Kill: 204 No Content.
        server.enqueue(MockResponse.Builder().code(204).build())
        holder.kill(row)
        joinChildren()

        assertTrue("session must be removed from list", holder.sessions.isEmpty())
        assertNull(
            "currentSession must be cleared when the killed row was current",
            holder.currentSession,
        )
    }

    // ── refreshSessions ──────────────────────────────────────────────────

    @Test
    fun `refreshSessions populates sessions and clears initial-load flag`() = runTest {
        val holder = newHolder(this)

        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """
                    {"sessions":[
                      {"name":"s1","provider":"claude"},
                      {"name":"s2","provider":"gemini","app":"x"}
                    ]}
                    """.trimIndent()
                )
                .build()
        )

        assertTrue("initial isInitialLoad", holder.isInitialLoad)
        holder.refreshSessions()
        joinChildren()

        assertEquals(2, holder.sessions.size)
        assertEquals("s1", holder.sessions[0].name)
        assertEquals("gemini", holder.sessions[1].provider)
        assertTrue(
            "isInitialLoad must clear after first refresh completes",
            !holder.isInitialLoad,
        )
    }

    @Test
    fun `refreshSessions clears currentSession when it disappears server-side`() = runTest {
        val holder = newHolder(this)

        // Seed: launch creates a session and sets it as current.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"gone-soon","session_url":"x","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"gone-soon","provider":"claude"}]}""")
                .build()
        )
        holder.launch("claude")
        joinChildren()
        assertNotNull(holder.currentSession)

        // Server-side disappearance (someone killed via API, TTL expired, etc).
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[]}""")
                .build()
        )
        holder.refreshSessions()
        joinChildren()

        assertNull(
            "currentSession must be cleared when the row is gone from server",
            holder.currentSession,
        )
        assertTrue(holder.sessions.isEmpty())
    }

    @Test
    fun `refreshSessions surfaces error via callback when server returns 500`() = runTest {
        val errors = mutableListOf<Pair<String, String>>()
        val holder = newHolder(this, errors = errors)

        server.enqueue(MockResponse.Builder().code(500).body("""{"detail":"x"}""").build())

        holder.refreshSessions()
        joinChildren()

        assertTrue("refresh error must surface", errors.isNotEmpty())
        assertEquals("refresh", errors[0].first)
        assertTrue(
            "sessions must remain empty (unchanged) on transient failure",
            holder.sessions.isEmpty(),
        )
        assertTrue(
            "isInitialLoad must clear even on failure so UI exits the loader",
            !holder.isInitialLoad,
        )
    }

    // ── selectSession / clearCurrent ─────────────────────────────────────

    // ── liveSessionFor (T22: per-name live ZellijSession breadcrumb) ─────

    @Test
    fun `liveSessionFor returns the launched ZellijSession after launch completes`() = runTest {
        val holder = newHolder(this)

        // Launch response carries token + session_url.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body(
                    """{"session_name":"Brandon__terminal___root__9","session_url":"https://x","token":"tok-xyz","expires_at":null}""",
                )
                .build()
        )
        // Follow-up refresh.
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"Brandon__terminal___root__9","provider":"terminal"}]}""")
                .build()
        )

        holder.launch("terminal")
        joinChildren()

        val live = holder.liveSessionFor("Brandon__terminal___root__9")
        assertNotNull("live session must be retained per-name after launch", live)
        assertEquals("tok-xyz", live?.token)
        assertEquals("https://x", live?.sessionUrl)
        assertEquals("terminal", live?.provider)
    }

    @Test
    fun `liveSessionFor returns null for sessions this holder never launched`() = runTest {
        val holder = newHolder(this)

        // Just a list call — no launch ever fires through this holder.
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"foreign","provider":"claude"}]}""")
                .build()
        )
        holder.refreshSessions()
        joinChildren()

        // Row exists in [sessions] but [liveSessionFor] knows nothing — this
        // is the path the screen uses to detect "no token, must relaunch."
        assertEquals(1, holder.sessions.size)
        assertNull(holder.liveSessionFor("foreign"))
    }

    @Test
    fun `kill drops the live session breadcrumb`() = runTest {
        val holder = newHolder(this)

        // Seed via launch so we have a live entry to drop.
        server.enqueue(
            MockResponse.Builder()
                .code(201)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"session_name":"doomed","session_url":"u","token":"t","expires_at":null}""")
                .build()
        )
        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"doomed","provider":"claude"}]}""")
                .build()
        )
        holder.launch("claude")
        joinChildren()
        assertNotNull(holder.liveSessionFor("doomed"))

        // Kill the session.
        server.enqueue(MockResponse.Builder().code(204).build())
        holder.kill(holder.sessions.first())
        joinChildren()

        assertNull(
            "Live-session breadcrumb must be dropped on kill (token's revoked server-side)",
            holder.liveSessionFor("doomed"),
        )
    }

    @Test
    fun `kill tears down the process-lived TerminalSessionManager client`() = runTest {
        val holder = newHolder(this)

        // Seed a live client in the manager for a session name, the way the
        // terminal screen does on mount. The factory points at an unreachable
        // origin (NOT the MockWebServer) so the client's background connect
        // coroutine fails fast on the manager's own scope without consuming
        // any of this test's queued MockWebServer responses.
        val deadApi = BlackBoxApi("http://127.0.0.1:1")
        TerminalSessionManager.clientFactory = { s, _, sc ->
            ZellijWebSocketClient(
                origin = deadApi.getBaseUrl(),
                sessionName = s.name,
                coroutineScope = sc,
            )
        }
        val live = ZellijSession(name = "managed", provider = "claude")
        val noop = object : ZellijWebSocketClient.Listener {
            override fun onConnected() {}
            override fun onBytes(bytes: ByteArray, length: Int) {}
            override fun onSwitchedSession(newSessionName: String) {}
            override fun onDisconnected(code: Int, reason: String, willReconnect: Boolean) {}
            override fun onError(throwable: Throwable) {}
        }
        val client = TerminalSessionManager.getOrConnect(live, deadApi, TerminalSessionManager.scope, noop)
        assertTrue(TerminalSessionManager.hasLiveClient("managed"))

        // Kill via the holder (the X-button path). Build the row directly so
        // we don't have to round-trip a launch (which would interleave with
        // the manager's connect coroutine). Backend DELETE = 204.
        val row = ZellijSessionRow(name = "managed", provider = "claude")
        server.enqueue(MockResponse.Builder().code(204).build())
        holder.kill(row)
        joinChildren()

        assertTrue("manager client must be closed after kill", client.isClosed())
        assertNull(
            "manager must no longer hold the live client after kill",
            TerminalSessionManager.liveClientFor("managed"),
        )
    }

    // ── selectSession / clearCurrent ─────────────────────────────────────

    @Test
    fun `selectSession sets and clearCurrent unsets currentSession`() = runTest {
        val holder = newHolder(this)

        server.enqueue(
            MockResponse.Builder()
                .code(200)
                .headers(headersOf("Content-Type", "application/json"))
                .body("""{"sessions":[{"name":"a","provider":"claude"},{"name":"b","provider":"gemini"}]}""")
                .build()
        )
        holder.refreshSessions()
        joinChildren()

        assertEquals(2, holder.sessions.size)
        assertNull(holder.currentSession)

        holder.selectSession(holder.sessions[1])
        assertEquals("b", holder.currentSession?.name)

        holder.clearCurrent()
        assertNull(holder.currentSession)
    }
}
