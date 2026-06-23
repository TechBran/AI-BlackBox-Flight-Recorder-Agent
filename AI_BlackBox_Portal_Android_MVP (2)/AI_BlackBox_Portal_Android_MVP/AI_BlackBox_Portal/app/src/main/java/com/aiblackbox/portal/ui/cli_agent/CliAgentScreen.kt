package com.aiblackbox.portal.ui.cli_agent

import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.PaddingValues
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.Scaffold
import androidx.compose.runtime.Composable
import androidx.compose.runtime.CompositionLocalProvider
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CliAgentProvider
import com.aiblackbox.portal.data.model.ZellijSession
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.insets.LocalShowAppChrome
import kotlinx.coroutines.launch

/**
 * Top-level CLI Agent flow.
 *
 * Phase 4 / T22 reshape — wires [SessionSwitcherTopBar] (T20) over BOTH
 * terminal branches and routes the zellij-launched Terminal branch through
 * the new [ZellijTerminalScreen] (driven by [com.aiblackbox.portal.data.api.ZellijWebSocketClient])
 * while preserving the legacy tmux [TerminalScreen] on the picker-driven
 * LegacyTerminal branch.
 *
 * State machine:
 *
 *   EmptyState                ← default landing; no current session.
 *      │  tap "+ Terminal" / shortcut → screenState.launch(provider)
 *      │  long-press "+ Terminal"      → FolderPicker
 *      ▼
 *   Terminal(ZellijSession)   ← zellij-backed via ZellijTerminalScreen.
 *      │  back → clearCurrent() → EmptyState
 *      ▼
 *   FolderPicker              ← legacy AppFolderPicker for workspace-pinned launches.
 *      │  pick app → LegacyTerminal
 *      ▼
 *   LegacyTerminal(...)       ← tmux-backed via TerminalScreen.
 *      │  back → EmptyState
 *      ▼
 *   (back from EmptyState → onBackToTools)
 *
 * **Switcher** (T20 [SessionSwitcherTopBar]) hosts the bar for BOTH terminal
 * branches and the empty state. The bar shows the current session's
 * provider+time label, drops down to a session list, exposes "+ Terminal" and
 * the shortcuts sub-menu, and emits intents back to the holder for
 * launch / select / kill. The hamburger inside the bar calls
 * [onOpenNavDrawer] which the caller wires to the existing SettingsSheet.
 *
 * **Token discipline (audit I7).** The launch response's `token` lives only
 * inside [CliAgentScreenState.liveSessionFor] and the [ZellijSession]
 * threaded into the Terminal state tuple. It's not persisted to disk and is
 * dropped on holder rebuild (operator switch, config change). Once the WS
 * client connects, the server holds state and the token becomes irrelevant.
 */
@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CliAgentScreen(
    origin: String,
    operator: String,
    onBackToTools: () -> Unit,
    onOpenNavDrawer: () -> Unit = {},
    /**
     * T23 device QA fix: invoked with `true` whenever the inner state
     * machine enters a terminal branch (Zellij or Legacy), `false`
     * otherwise. NativeMainActivity uses the flag to hide its
     * activity-scope chrome layers (operator pill + Layer 2.5 X close
     * button) so they don't overlap [SessionSwitcherTopBar]. The earlier
     * T20 `LocalShowAppChrome` CompositionLocal pattern only reached
     * descendants of CliAgentScreen — siblings like BlackBoxTopBar in
     * NativeMainActivity never saw the override.
     */
    onTerminalActiveChange: (Boolean) -> Unit = {},
    modifier: Modifier = Modifier,
) {
    val context = LocalContext.current
    val store = remember { BlackBoxStore(context) }
    val scope = rememberCoroutineScope()

    val api: BlackBoxApi = remember(origin) { BlackBoxApi(origin) }
    val repository = remember(api) { CliAgentSessionRepository(api) }

    val providerSlug by store.cliAgentProviderFlow
        .collectAsState(initial = CliAgentProvider.CLAUDE.slug)
    val selectedProvider = remember(providerSlug) {
        CliAgentProvider.fromSlug(providerSlug)
    }

    // ── Screen-scoped state holder ───────────────────────────────────────
    //
    // Holder is per-operator; rebuilds when [operator] changes via
    // `remember(operator, repository)`. The Terminal-state tuple receives
    // the freshly-launched ZellijSession in [onLaunched]; selecting an
    // existing row from the switcher goes through [liveSessionFor] to find
    // the matching token-carrying session (or toasts if none — see below).
    var state by remember { mutableStateOf<CliAgentInternalState>(CliAgentInternalState.EmptyState) }
    val screenState = remember(operator, repository) {
        CliAgentScreenState(
            scope = scope,
            repository = repository,
            operator = operator,
            onLaunched = { session ->
                // Transition into Terminal with the fully-credentialed
                // ZellijSession. Token + sessionUrl ride along until the
                // composable mounts and the ZellijWebSocketClient consumes
                // them on connect.
                state = CliAgentInternalState.Terminal(session)
            },
            onError = { action, reason ->
                Toast.makeText(
                    context,
                    "CLI Agent ${action} failed: $reason",
                    Toast.LENGTH_SHORT,
                ).show()
            },
        )
    }

    // Initial fetch + refresh when operator changes.
    LaunchedEffect(operator) {
        screenState.refreshSessions()
    }

    // Hide the floating app chrome while a terminal session is on screen.
    //
    // T23 fix: the CompositionLocalProvider scoping only reaches descendants
    // of CliAgentScreen — sibling overlays in NativeMainActivity (Layer 2
    // BlackBoxTopBar + Layer 2.5 X close button) never saw the override and
    // stayed visible on real devices. The provider is kept for any future
    // CliAgentScreen-scoped consumers, but the activity-level hide now goes
    // through [onTerminalActiveChange] which NativeMainActivity treats as
    // a hard override.
    val terminalActive = state is CliAgentInternalState.Terminal ||
        state is CliAgentInternalState.LegacyTerminal
    LaunchedEffect(terminalActive) {
        onTerminalActiveChange(terminalActive)
    }
    CompositionLocalProvider(LocalShowAppChrome provides !terminalActive) {
        // The session switcher top bar is shown on every branch EXCEPT the
        // pure FolderPicker (which has its own legacy chrome). Showing it
        // over EmptyState too is intentional — even with no current session
        // the user can fire "+ Terminal" or jump to an existing session
        // from the dropdown.
        val showSwitcher = state !is CliAgentInternalState.FolderPicker

        Scaffold(
            modifier = modifier.fillMaxSize(),
            topBar = {
                if (showSwitcher) {
                    SessionSwitcherTopBar(
                        operator = operator,
                        currentSession = screenState.currentSession,
                        sessions = screenState.sessions,
                        launchInFlight = screenState.launchInFlight,
                        onSelectSession = { row ->
                            // Phase 1 (2026-06-22, session persistence): reattach
                            // via [TerminalSessionManager] instead of toasting
                            // "kill and relaunch". Resolution order:
                            //   1. A live ZellijSession breadcrumb this holder
                            //      launched (carries token/url) — best case.
                            //   2. Otherwise synthesise a minimal ZellijSession
                            //      from the row. Under the master-token proxy
                            //      model the name alone is enough to (re)open the
                            //      proxy WS; if the manager already holds a live
                            //      client for that name, getOrConnect rebinds it
                            //      (no new POST /session, no new socket).
                            // Either path lands in Terminal state; the
                            // ZellijTerminalScreen mount calls
                            // TerminalSessionManager.getOrConnect with this
                            // session, which reuses the live client when present.
                            screenState.selectSession(row)
                            val live = screenState.liveSessionFor(row.name)
                                ?: ZellijSession(
                                    name = row.name,
                                    provider = row.provider,
                                    app = row.app,
                                    createdAt = row.createdAt,
                                    expiresAt = row.expiresAt,
                                )
                            state = CliAgentInternalState.Terminal(live)
                        },
                        onLaunchProvider = { provider ->
                            // Launch path — onLaunched callback drives the
                            // state transition into Terminal once the
                            // launch response is in.
                            screenState.launch(provider)
                        },
                        onKillSession = { row ->
                            // The X button is the ONLY kill path (Phase 1).
                            // screenState.kill -> TerminalSessionManager.kill
                            // (closes the socket, drops from the live map) +
                            // backend DELETE. If we're killing the session we're
                            // currently viewing, fall back to EmptyState so the
                            // screen doesn't show a stale terminal surface.
                            val current = state
                            val killingCurrent =
                                current is CliAgentInternalState.Terminal &&
                                    current.session.name == row.name
                            screenState.kill(row)
                            if (killingCurrent) {
                                state = CliAgentInternalState.EmptyState
                            }
                        },
                        onOpenNavDrawer = onOpenNavDrawer,
                    )
                }
            },
        ) { innerPadding ->
            CliAgentBranches(
                state = state,
                onStateChange = { state = it },
                screenState = screenState,
                repository = repository,
                api = api,
                operator = operator,
                selectedProvider = selectedProvider,
                onProviderSelected = { p ->
                    scope.launch { store.setCliAgentProvider(p.slug) }
                },
                onBackToTools = onBackToTools,
                innerPadding = innerPadding,
            )
        }
    }
}

/**
 * The branch switch is split out so the Scaffold body stays slim and the
 * branches each get a clean `Modifier.padding(innerPadding)` for the
 * SessionSwitcherTopBar inset. Sealed-class exhaustiveness check makes a
 * future state add a compile-time push (no silent missed branch).
 */
@Composable
private fun CliAgentBranches(
    state: CliAgentInternalState,
    onStateChange: (CliAgentInternalState) -> Unit,
    screenState: CliAgentScreenState,
    repository: CliAgentSessionRepository,
    api: BlackBoxApi,
    operator: String,
    selectedProvider: CliAgentProvider,
    onProviderSelected: (CliAgentProvider) -> Unit,
    onBackToTools: () -> Unit,
    innerPadding: PaddingValues,
) {
    when (val s = state) {
        CliAgentInternalState.EmptyState -> {
            BackHandler(enabled = true) { onBackToTools() }
            Box(modifier = Modifier.fillMaxSize().padding(innerPadding)) {
                CliAgentEmptyState(
                    launchInFlight = screenState.launchInFlight,
                    onLaunchProvider = { provider ->
                        screenState.launch(provider)
                    },
                    onChooseFolderForTerminal = {
                        onStateChange(CliAgentInternalState.FolderPicker)
                    },
                    modifier = Modifier.fillMaxSize(),
                )
            }
        }

        CliAgentInternalState.FolderPicker -> {
            BackHandler(enabled = true) {
                onStateChange(CliAgentInternalState.EmptyState)
            }
            // FolderPicker intentionally renders without the SessionSwitcherTopBar
            // (showSwitcher = false above) so we don't need innerPadding here —
            // Scaffold's body still wraps it but with no topBar inset.
            AppFolderPicker(
                repository = repository,
                operator = operator,
                selectedProvider = selectedProvider,
                onProviderSelected = onProviderSelected,
                onAppSelected = { slug, name ->
                    onStateChange(
                        CliAgentInternalState.LegacyTerminal(
                            appSlug = slug,
                            appName = name,
                            provider = selectedProvider.slug,
                        )
                    )
                },
                modifier = Modifier.fillMaxSize(),
            )
        }

        is CliAgentInternalState.Terminal -> {
            // Defensive: if currentSession was cleared from under us (kill
            // race), bounce back to EmptyState. The Terminal-state tuple is
            // independent of currentSession, but they should agree.
            LaunchedEffect(screenState.currentSession?.name) {
                if (screenState.currentSession == null) {
                    onStateChange(CliAgentInternalState.EmptyState)
                }
            }
            // T23 device QA fix (2026-05-26): wrap in key(session.name) so
            // switching sessions via the switcher tears down the ENTIRE
            // ZellijTerminalScreen subtree and rebuilds it fresh — new
            // ZellijWebSocketClient, new TerminalView, new TerminalEmulator,
            // new pointerInput gesture handler. Without this key, the
            // composable just re-runs with new params: `remember(session.name)`
            // returns a fresh client, but the pointerInput(Unit) lambda,
            // AndroidView factory closures, and other `remember { }` slots
            // retain stale captures of the previous session's client.
            // Symptom Brandon hit: scroll (and probably typing) stops
            // working after switching sessions because the gesture handler
            // calls .sendBytes() on a closed WebSocket. Wrapping in key()
            // also avoids mixing the new session's bytes into the old
            // session's TerminalEmulator scrollback (visual contamination).
            androidx.compose.runtime.key(s.session.name) {
                ZellijTerminalScreen(
                    api = api,
                    operator = operator,
                    session = s.session,
                    onBack = {
                        // Phase 1: back DETACHES, never kills. The live client
                        // stays in TerminalSessionManager (socket alive) and the
                        // session row stays in [sessions], so the switcher can
                        // reattach. clearCurrent() only drops the top-bar's
                        // "current" selection — it does NOT close the socket or
                        // drop the manager's live client.
                        screenState.clearCurrent()
                        onStateChange(CliAgentInternalState.EmptyState)
                    },
                    modifier = Modifier.fillMaxSize().padding(innerPadding),
                )
            }
        }

        is CliAgentInternalState.LegacyTerminal -> {
            // Backwards-compat: tmux-backed legacy path, unchanged from
            // before T22. Kept verbatim so the picker → tmux flow keeps
            // working end-to-end during the migration.
            TerminalScreen(
                api = api,
                operator = operator,
                appSlug = s.appSlug,
                appName = s.appName,
                provider = s.provider,
                onBack = { onStateChange(CliAgentInternalState.EmptyState) },
                modifier = Modifier.fillMaxSize().padding(innerPadding),
            )
        }
    }
}

internal sealed class CliAgentInternalState {
    /** No active session — show empty-state launch UI. */
    data object EmptyState : CliAgentInternalState()

    /** Long-press fallback — legacy app/folder picker. */
    data object FolderPicker : CliAgentInternalState()

    /**
     * Zellij-launched terminal session.
     *
     * Carries the full [ZellijSession] (with `token` + `sessionUrl`) so
     * [ZellijTerminalScreen] can construct a [com.aiblackbox.portal.data.api.ZellijWebSocketClient]
     * directly from this tuple. Token lives only here + in
     * [CliAgentScreenState.liveSessionFor]; it's never persisted to disk
     * (audit I7).
     */
    data class Terminal(val session: ZellijSession) : CliAgentInternalState()

    /** Picker-driven legacy terminal (tmux path with explicit app pick). */
    data class LegacyTerminal(
        val appSlug: String,
        val appName: String,
        val provider: String,
    ) : CliAgentInternalState()
}
