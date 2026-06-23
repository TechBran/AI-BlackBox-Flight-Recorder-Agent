package com.aiblackbox.portal.ui.cli_agent

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.ZellijWebSocketClient
import com.aiblackbox.portal.data.model.ZellijSession
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import java.util.concurrent.atomic.AtomicInteger

/**
 * Phase 1 unit tests for [TerminalSessionManager] — the process-lived owner
 * of terminal connections that makes sessions survive navigation.
 *
 * The manager is exercised WITHOUT opening real sockets by overriding its
 * [TerminalSessionManager.clientFactory] with one that constructs a real
 * [ZellijWebSocketClient] (against an unreachable origin, so its background
 * connect coroutine fails harmlessly without ever throwing synchronously or
 * setting `userClosed`). Counting factory invocations is exactly counting
 * "new POST /session + new socket", because the manager calls
 * [ZellijWebSocketClient.connect] once per freshly-constructed client.
 *
 * Coverage (per the Phase 1 plan):
 *   (a) same live client returned for a session name across a simulated
 *       dispose -> recreate (no second construct/connect).
 *   (b) detach (the dispose path) does NOT close the socket; only kill does.
 *   (c) the switcher reattach path reuses a live client without a new
 *       construct/connect (rebind, not reconnect).
 *   (d) kill removes from the manager AND closes the client.
 */
class TerminalSessionManagerTest {

    private val scope: CoroutineScope =
        CoroutineScope(SupervisorJob() + Dispatchers.Unconfined)

    private val api = BlackBoxApi("http://127.0.0.1:1") // unreachable on purpose

    /** Counts how many clients the manager constructed (== connect calls). */
    private val constructCount = AtomicInteger(0)

    private val noopListener = object : ZellijWebSocketClient.Listener {
        override fun onConnected() {}
        override fun onBytes(bytes: ByteArray, length: Int) {}
        override fun onSwitchedSession(newSessionName: String) {}
        override fun onDisconnected(code: Int, reason: String, willReconnect: Boolean) {}
        override fun onError(throwable: Throwable) {}
    }

    private fun session(name: String, provider: String = "claude") = ZellijSession(
        name = name,
        provider = provider,
        sessionUrl = "http://x",
        token = "t",
    )

    @Before
    fun setUp() {
        TerminalSessionManager.resetForTest()
        constructCount.set(0)
        // Count constructions; still return a real client so isClosed() /
        // close() behave exactly as production.
        TerminalSessionManager.clientFactory = { s, a, sc ->
            constructCount.incrementAndGet()
            ZellijWebSocketClient(
                origin = a.getBaseUrl(),
                sessionName = s.name,
                coroutineScope = sc,
            )
        }
    }

    @After
    fun tearDown() {
        TerminalSessionManager.resetForTest()
    }

    // (a) -----------------------------------------------------------------

    @Test
    fun `getOrConnect returns the same live client across dispose then recreate`() {
        val s = session("Brandon__claude___root__1")

        // First mount.
        val c1 = TerminalSessionManager.getOrConnect(s, api, scope, noopListener)
        // Simulate the screen leaving composition (back nav).
        TerminalSessionManager.detach(s.name, cols = 100, rows = 40)
        // Re-enter the same session.
        val c2 = TerminalSessionManager.getOrConnect(s, api, scope, noopListener)

        assertSame("Same session name must hand back the same live client", c1, c2)
        assertEquals(
            "A reattach must NOT construct/connect a second client (no new POST /session)",
            1,
            constructCount.get(),
        )
        assertFalse("Reused client must not be closed", c2.isClosed())
    }

    // (b) -----------------------------------------------------------------

    @Test
    fun `detach does not close the socket — only kill does`() {
        val s = session("Brandon__claude___root__2")
        val c = TerminalSessionManager.getOrConnect(s, api, scope, noopListener)

        TerminalSessionManager.detach(s.name)

        assertFalse("detach must NOT close the client", c.isClosed())
        assertTrue("client must still be held after detach", TerminalSessionManager.hasLiveClient(s.name))
        assertFalse(
            "renderBound must be false after detach (renderer unbound)",
            TerminalSessionManager.renderBoundForTest(s.name) ?: true,
        )

        // Now an explicit kill DOES close it.
        TerminalSessionManager.kill(s.name)
        assertTrue("kill must close the client", c.isClosed())
    }

    // (c) -----------------------------------------------------------------

    @Test
    fun `switcher reattach reuses the live client without a new connect`() {
        val s = session("Brandon__claude___root__3")

        // Initial connect (e.g. just-launched session, screen open).
        TerminalSessionManager.getOrConnect(s, api, scope, noopListener)
        TerminalSessionManager.detach(s.name) // user navigated away

        assertTrue(
            "manager still holds the live client while detached",
            TerminalSessionManager.hasLiveClient(s.name),
        )

        // Switcher row select -> screen mounts -> getOrConnect again. Under
        // the master-token model the row carries only name+provider, so the
        // screen synthesises a minimal ZellijSession; the NAME matches, so
        // the manager must rebind the existing live client.
        val rowSession = session(s.name) // simulates synthesised-from-row
        val reattached = TerminalSessionManager.getOrConnect(rowSession, api, scope, noopListener)

        assertEquals(
            "Switcher reattach must reuse the live client (no second construct/connect)",
            1,
            constructCount.get(),
        )
        assertFalse(reattached.isClosed())
        assertTrue(
            "renderBound must be true again after reattach",
            TerminalSessionManager.renderBoundForTest(s.name) ?: false,
        )
    }

    @Test
    fun `getOrConnect after kill constructs a fresh client`() {
        val s = session("Brandon__claude___root__4")
        TerminalSessionManager.getOrConnect(s, api, scope, noopListener)
        TerminalSessionManager.kill(s.name)

        // After an explicit kill the name is gone; re-opening it is a genuine
        // new session and SHOULD construct/connect again.
        TerminalSessionManager.getOrConnect(s, api, scope, noopListener)
        assertEquals(
            "A getOrConnect after kill must construct a new client",
            2,
            constructCount.get(),
        )
    }

    // (d) -----------------------------------------------------------------

    @Test
    fun `kill removes from the manager and closes the client`() {
        val s = session("Brandon__claude___root__5")
        val c = TerminalSessionManager.getOrConnect(s, api, scope, noopListener)

        assertTrue(TerminalSessionManager.activeNames().contains(s.name))

        val killed = TerminalSessionManager.kill(s.name)

        assertTrue("kill must report it tore down a held client", killed)
        assertTrue("client must be closed after kill", c.isClosed())
        assertNull("liveClientFor must be null after kill", TerminalSessionManager.liveClientFor(s.name))
        assertFalse("name must be gone from activeNames after kill", TerminalSessionManager.activeNames().contains(s.name))
    }

    @Test
    fun `kill on an unknown name is a harmless no-op`() {
        assertFalse(
            "killing a name the manager never held returns false",
            TerminalSessionManager.kill("never-existed"),
        )
    }

    // bookkeeping ----------------------------------------------------------

    @Test
    fun `activeNames and activeCount reflect held live sessions`() {
        TerminalSessionManager.getOrConnect(session("a"), api, scope, noopListener)
        TerminalSessionManager.getOrConnect(session("b"), api, scope, noopListener)

        assertEquals(2, TerminalSessionManager.activeCount())
        assertEquals(setOf("a", "b"), TerminalSessionManager.activeNames())

        TerminalSessionManager.kill("a")
        assertEquals(1, TerminalSessionManager.activeCount())
        assertEquals(setOf("b"), TerminalSessionManager.activeNames())
    }

    @Test
    fun `detach records last-known cols and rows`() {
        val s = session("Brandon__claude___root__6")
        TerminalSessionManager.getOrConnect(s, api, scope, noopListener)
        TerminalSessionManager.detach(s.name, cols = 132, rows = 50)

        val entry = TerminalSessionManager.liveEntryForTest(s.name)
        assertEquals(132, entry?.cols)
        assertEquals(50, entry?.rows)
    }
}
