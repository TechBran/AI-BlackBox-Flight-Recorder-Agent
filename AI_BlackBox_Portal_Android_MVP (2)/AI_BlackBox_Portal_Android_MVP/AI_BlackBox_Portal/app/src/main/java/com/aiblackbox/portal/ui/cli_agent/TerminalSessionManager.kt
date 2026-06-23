package com.aiblackbox.portal.ui.cli_agent

import android.util.Log
import androidx.annotation.VisibleForTesting
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.ZellijWebSocketClient
import com.aiblackbox.portal.data.model.ZellijSession
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.SupervisorJob
import java.util.concurrent.ConcurrentHashMap

/**
 * Process-lived owner of live terminal connections (Phase 1 of the
 * terminal-session-persistence work — see
 * docs/plans/2026-06-22-zellij-terminal-session-persistence.md).
 *
 * ## Why a process singleton
 *
 * Before this, the [ZellijWebSocketClient] was `remember(session.name)`'d
 * inside [ZellijTerminalScreen] and the whole subtree was wrapped in
 * `key(session.name)` in [CliAgentScreen]. On back navigation the
 * `cli_agent` route is popped, the Composable leaves composition, its
 * `DisposableEffect.onDispose` ran `client.close()` (which sets `userClosed`
 * permanently, defeating reconnect), and the only reattach handle
 * (screen-scoped `liveSessionsByName`) was dropped. Net effect: the live
 * server session was orphaned and re-entry always minted a NEW one.
 *
 * This manager hoists connection + session-handle ownership OUT of the
 * composition into a process-scoped `object` so:
 *   - Leaving the screen **detaches** the renderer (stops forwarding bytes
 *     to a dead [com.termux.view.TerminalView]) but **never closes** the
 *     socket.
 *   - Returning to the same session **reuses** the live client (no new
 *     `POST /session`, no new socket) by swapping the listener in place.
 *   - The ONLY teardown path that closes a socket is [kill] (the explicit
 *     X button), which also issues the backend DELETE via the caller.
 *
 * It is intentionally NOT tied to a Compose screen or a `NavBackStackEntry`
 * (the `cli_agent` route is popped on back). It is a plain `object`; a
 * later phase (Phase 3) anchors its lifetime to a foreground service so the
 * process stays warm in the background, but the API here is independent of
 * that.
 *
 * ## Thread-safety
 *
 * Accessed from the UI thread (mount/dispose, switcher taps) AND from
 * OkHttp callback threads (indirectly, via the clients it holds). The
 * client map is a [ConcurrentHashMap]; the compound get-or-create in
 * [getOrConnect] and the remove-and-tear-down in [kill] are additionally
 * `synchronized(lock)` so two concurrent callers can't double-construct or
 * race a create against a kill.
 */
object TerminalSessionManager {

    private const val TAG = "TerminalSessionMgr"

    /**
     * Factory producing a (not-yet-connected) [ZellijWebSocketClient] for a
     * session. Overridable for unit tests so the manager can be exercised
     * without opening real sockets. The default builds the production client
     * exactly as [ZellijTerminalScreen] used to inline.
     */
    @Volatile
    @VisibleForTesting
    internal var clientFactory: (ZellijSession, BlackBoxApi, CoroutineScope) -> ZellijWebSocketClient =
        { session, api, scope ->
            ZellijWebSocketClient(
                origin = api.getBaseUrl(),
                sessionName = session.name,
                coroutineScope = scope,
            )
        }

    /**
     * One live entry per session name. Holds the durable client plus the
     * metadata needed to reattach and the last-known grid size so a later
     * phase can replay it on cold reattach.
     */
    internal class LiveClient(
        val client: ZellijWebSocketClient,
        /** Full handle (name/provider/token/url) captured at first connect. */
        @Volatile var session: ZellijSession,
        @Volatile var cols: Int = DEFAULT_COLS,
        @Volatile var rows: Int = DEFAULT_ROWS,
        /** True while a renderer is currently bound (Composable in composition). */
        @Volatile var renderBound: Boolean = false,
    )

    private val clients = ConcurrentHashMap<String, LiveClient>()
    private val lock = Any()

    /**
     * Process-lived scope handed to the clients this manager creates. MUST
     * outlive any single Composable so the client's IO + reconnect coroutines
     * survive navigation away from the terminal screen. A [SupervisorJob] so
     * one client's failure can't cancel the others. Never cancelled in
     * Phase 1 (the process owns it); Phase 3's foreground service may own it.
     */
    val scope: CoroutineScope = CoroutineScope(SupervisorJob() + Dispatchers.Default)

    /**
     * Return the live client for [session]'s name, creating + connecting one
     * if none exists. If a live client already exists for the name, the
     * existing socket is **reused** and [listener] is re-bound in place (no
     * new `POST /session`, no new socket). Either way the renderer is now
     * bound and rendering resumes.
     *
     * @param session full handle for the session (token/url ride along for
     *   forward-compat; Phase 1 uses name + master-token proxy auth).
     * @param api used to derive the WS origin and (later) for reattach REST.
     * @param scope long-lived scope for the client's coroutines. MUST NOT be
     *   a `rememberCoroutineScope()` that dies with the Composable — pass a
     *   process/service-scoped scope so reconnect survives navigation. (The
     *   client only uses it for IO + reconnect; the manager keeps the client
     *   alive regardless.)
     * @param listener the (fresh) renderer to forward bytes to.
     */
    fun getOrConnect(
        session: ZellijSession,
        api: BlackBoxApi,
        scope: CoroutineScope,
        listener: ZellijWebSocketClient.Listener,
    ): ZellijWebSocketClient {
        synchronized(lock) {
            val existing = clients[session.name]
            if (existing != null && !existing.client.isClosed()) {
                Log.d(TAG, "getOrConnect: reusing live client for '${session.name}' (rebind)")
                existing.session = session
                existing.renderBound = true
                existing.client.rebindListener(listener)
                return existing.client
            }
            // None live (or the prior one was killed) -> build + connect fresh.
            Log.d(TAG, "getOrConnect: creating new client for '${session.name}'")
            val client = clientFactory(session, api, scope)
            val live = LiveClient(client = client, session = session, renderBound = true)
            clients[session.name] = live
            client.connect(listener)
            return client
        }
    }

    /**
     * Stop rendering for [name] WITHOUT closing the socket. Called from the
     * Composable's `onDispose` on navigation away. The live client stays in
     * the map and the socket keeps flowing (the reconnect machinery stays
     * usable). A later [getOrConnect] for the same name re-binds it.
     *
     * Records [cols]/[rows] as the last-known grid size for the session so a
     * future cold reattach can replay them.
     */
    fun detach(name: String, cols: Int? = null, rows: Int? = null) {
        val live = clients[name] ?: return
        if (cols != null) live.cols = cols
        if (rows != null) live.rows = rows
        live.renderBound = false
        Log.d(TAG, "detach: '$name' renderer unbound, socket kept alive")
        live.client.detach()
    }

    /**
     * Explicit kill — the ONLY path that closes a socket. Sends the client's
     * permanent [ZellijWebSocketClient.close] (disables reconnect, closes
     * both sockets) and removes the entry from the map. The backend DELETE
     * is the caller's responsibility (it owns the operator-scoped REST call);
     * this just tears down the client side.
     *
     * @return true if a live client existed and was torn down; false if the
     *   name wasn't held (idempotent — safe to call after a server-side
     *   disappearance).
     */
    fun kill(name: String): Boolean {
        val live = synchronized(lock) { clients.remove(name) } ?: return false
        Log.d(TAG, "kill: '$name' — closing socket + dropping from map")
        try {
            live.client.close()
        } catch (t: Throwable) {
            Log.w(TAG, "kill: client.close() threw for '$name'", t)
        }
        live.renderBound = false
        return true
    }

    /** The live client for [name], or null if none is held (or it was killed). */
    fun liveClientFor(name: String): ZellijWebSocketClient? =
        clients[name]?.takeIf { !it.client.isClosed() }?.client

    /** True if a (non-killed) live client is held for [name]. */
    fun hasLiveClient(name: String): Boolean = liveClientFor(name) != null

    /** Names of all currently-held live sessions. */
    fun activeNames(): Set<String> =
        clients.entries.filter { !it.value.client.isClosed() }.map { it.key }.toSet()

    /** Number of currently-held live sessions (for a future FGS notification). */
    fun activeCount(): Int = activeNames().size

    // --- Test hooks -------------------------------------------------------

    /** Reset all state — TEST ONLY (each test should start from empty). */
    @VisibleForTesting
    internal fun resetForTest() {
        synchronized(lock) {
            clients.values.forEach { try { it.client.close() } catch (_: Throwable) {} }
            clients.clear()
            clientFactory = { session, api, scope ->
                ZellijWebSocketClient(
                    origin = api.getBaseUrl(),
                    sessionName = session.name,
                    coroutineScope = scope,
                )
            }
        }
    }

    @VisibleForTesting
    internal fun renderBoundForTest(name: String): Boolean? = clients[name]?.renderBound

    @VisibleForTesting
    internal fun liveEntryForTest(name: String): LiveClient? = clients[name]

    const val DEFAULT_COLS = 80
    const val DEFAULT_ROWS = 24
}
