package com.aiblackbox.portal.ui.cli_agent

import android.content.Context
import android.util.Log
import androidx.annotation.VisibleForTesting
import com.aiblackbox.portal.TerminalForegroundService
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.ZellijWebSocketClient
import com.aiblackbox.portal.data.model.ZellijSession
import com.termux.terminal.TerminalSession
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
     * Application context, set once at process startup by
     * [com.aiblackbox.portal.PortalApplication.onCreate] via [init]. The manager
     * is a context-less `object`, so it cannot start a [TerminalForegroundService]
     * on its own — this is the handle it uses to drive the FGS on count
     * transitions. NULL until [init] runs; every service call below is guarded so
     * that (a) unit tests that never call [init] never touch a real service, and
     * (b) a very-early count change (before startup wiring) is a silent no-op
     * rather than a crash. Stored as the APPLICATION context (never an Activity)
     * so it is safe to retain for the process lifetime.
     */
    @Volatile
    private var appContext: Context? = null

    /**
     * Wire the manager to the process so it can keep terminals warm in the
     * background. Called ONCE from [com.aiblackbox.portal.PortalApplication.onCreate]
     * with the Application context. Idempotent and best-effort; safe to call
     * before any session exists. Until this runs, FGS drive is skipped (no crash).
     */
    fun init(appContext: Context) {
        this.appContext = appContext.applicationContext
    }

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
        /**
         * The Termux session that owns the [com.termux.terminal.TerminalEmulator]
         * — and therefore the scrollback transcript. Persisted here (alongside the
         * socket) so the emulator + its history survive a detach/reattach: on
         * re-entry the fresh [com.termux.view.TerminalView] re-links to THIS
         * session via attachSession, keeping scrollback intact. Created lazily by
         * the screen's AndroidView factory through [getOrCreateTerminalSession];
         * its PTY child is reaped in [kill].
         */
        @Volatile var terminalSession: TerminalSession? = null,
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
            // This is the only path that INCREASES the live count, so it drives the
            // foreground service. Snapshot the count BEFORE inserting so we can
            // detect the 0 -> 1 transition (start the FGS) vs. n -> n+1 (just update).
            Log.d(TAG, "getOrConnect: creating new client for '${session.name}'")
            val before = activeCount()
            val client = clientFactory(session, api, scope)
            val live = LiveClient(client = client, session = session, renderBound = true)
            clients[session.name] = live
            client.connect(listener)
            onLiveCountChanged(before, activeCount())
            return client
        }
    }

    /**
     * Get the persisted Termux [TerminalSession] for [name], creating it via
     * [create] on first call and caching it on the live client so its emulator
     * (and 2000-row scrollback transcript) survive navigation away + back. On
     * re-entry the SAME session is returned, so the screen's fresh
     * [com.termux.view.TerminalView] re-links to the existing emulator (history
     * intact) instead of building an empty one.
     *
     * The screen always calls [getOrConnect] before its AndroidView factory
     * runs, so a live client is normally held here; if somehow none is, the
     * created session is returned UN-persisted (degrades to the old per-mount
     * behavior) rather than crashing. Keyed by [name], so two different
     * sessions never share an emulator (no scrollback cross-contamination).
     */
    fun getOrCreateTerminalSession(name: String, create: () -> TerminalSession): TerminalSession {
        synchronized(lock) {
            val live = clients[name]
            val existing = live?.terminalSession
            if (existing != null) {
                Log.d(TAG, "getOrCreateTerminalSession: reusing persisted session for '$name' (scrollback preserved)")
                return existing
            }
            val created = create()
            if (live != null) {
                live.terminalSession = created
            } else {
                Log.w(TAG, "getOrCreateTerminalSession: no live client for '$name'; session NOT persisted")
            }
            return created
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
        // Take a COHERENT before/after snapshot under the SAME lock as
        // getOrConnect so a concurrent launch+kill (or double-kill) can't
        // compute overlapping counts and deliver a STOP that races a START
        // (which would tear down the FGS while a session is live). The service
        // drive fires AFTER releasing the lock so we never hold it across a
        // Binder call. (Delivery ordering across threads is still not
        // guaranteed — TerminalForegroundService.ACTION_STOP re-checks the live
        // count to self-heal a reordered STOP.)
        val (live, before, after) = synchronized(lock) {
            val b = activeCount()
            val removed = clients.remove(name) ?: return false
            Triple(removed, b, activeCount())
        }
        Log.d(TAG, "kill: '$name' — closing socket + dropping from map")
        try {
            live.client.close()
        } catch (t: Throwable) {
            Log.w(TAG, "kill: client.close() threw for '$name'", t)
        }
        live.renderBound = false
        // Reap the persisted Termux session's PTY child (the local `sleep`) and
        // release its emulator + scrollback — this session is gone for good.
        try {
            live.terminalSession?.finishIfRunning()
        } catch (t: Throwable) {
            Log.w(TAG, "kill: terminalSession.finishIfRunning() threw for '$name'", t)
        }
        live.terminalSession = null
        // n -> 0 stops the FGS, n -> n-1 updates its notification.
        onLiveCountChanged(before, after)
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

    /** Number of currently-held live sessions (for the FGS notification). */
    fun activeCount(): Int = activeNames().size

    // --- Foreground-service drive (Phase 3) -------------------------------

    /**
     * Abstraction over [TerminalForegroundService]'s start/stop/update so the
     * count-transition logic can be unit-tested without a real Android service.
     * Production uses [RealServiceController] (guarded on [appContext]); tests
     * swap in a recording fake. SAM-friendly: three best-effort, never-throwing ops.
     */
    @VisibleForTesting
    internal interface ServiceController {
        fun start()
        fun update()
        fun stop()
    }

    /**
     * Default controller: drives the real [TerminalForegroundService], but only
     * when [appContext] has been set ([init]). Before startup wiring — and in
     * unit tests that never call [init] — every op is a silent no-op, so the
     * manager never tries to start a real service off the JVM/Robolectric path.
     */
    private object RealServiceController : ServiceController {
        override fun start() { appContext?.let { TerminalForegroundService.start(it) } }
        override fun update() { appContext?.let { TerminalForegroundService.updateCount(it) } }
        override fun stop() { appContext?.let { TerminalForegroundService.stop(it) } }
    }

    @Volatile
    @VisibleForTesting
    internal var serviceController: ServiceController = RealServiceController

    /**
     * Drive the foreground service from a live-count transition. The only three
     * transitions that matter:
     *  - 0 -> >=1 : first terminal opened — START the FGS.
     *  - >=1 -> 0 : last terminal killed — STOP the FGS.
     *  - any other change while live (n -> m, both >= 1) : UPDATE the count.
     * All calls are best-effort (the controller swallows platform refusals) and
     * skipped entirely when no context is set, so the manager's core
     * detach/getOrConnect/kill semantics are unaffected if the FGS can't run.
     */
    private fun onLiveCountChanged(before: Int, after: Int) {
        if (before == after) return
        try {
            when {
                before == 0 && after > 0 -> serviceController.start()
                before > 0 && after == 0 -> serviceController.stop()
                after > 0 -> serviceController.update()
            }
        } catch (t: Throwable) {
            // Defensive: a controller op must never break a connect/kill.
            Log.w(TAG, "onLiveCountChanged: service drive threw (${t.javaClass.simpleName})")
        }
    }

    // --- Test hooks -------------------------------------------------------

    /** Reset all state — TEST ONLY (each test should start from empty). */
    @VisibleForTesting
    internal fun resetForTest() {
        synchronized(lock) {
            clients.values.forEach {
                try { it.client.close() } catch (_: Throwable) {}
                try { it.terminalSession?.finishIfRunning() } catch (_: Throwable) {}
            }
            clients.clear()
            clientFactory = { session, api, scope ->
                ZellijWebSocketClient(
                    origin = api.getBaseUrl(),
                    sessionName = session.name,
                    coroutineScope = scope,
                )
            }
            // Restore FGS-drive defaults so one test's fake controller / wired
            // context can't leak into the next (every test starts un-driven).
            serviceController = RealServiceController
            appContext = null
        }
    }

    @VisibleForTesting
    internal fun renderBoundForTest(name: String): Boolean? = clients[name]?.renderBound

    @VisibleForTesting
    internal fun liveEntryForTest(name: String): LiveClient? = clients[name]

    const val DEFAULT_COLS = 80
    const val DEFAULT_ROWS = 24
}
