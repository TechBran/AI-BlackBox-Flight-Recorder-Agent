package com.aiblackbox.portal.ui.cli_agent

import android.widget.Toast
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.fillMaxSize
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
import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.ui.insets.LocalShowAppChrome
import kotlinx.coroutines.launch

/**
 * Top-level CLI Agent flow.
 *
 * Phase 4 / T21 reshape of the original picker → terminal state machine:
 *
 *   EmptyState            ← default landing; no current session.
 *      │  tap "+ Terminal" / shortcut button → launch via [CliAgentScreenState]
 *      │  long-press "+ Terminal" → FolderPicker
 *      ▼
 *   Terminal              ← driven by [CliAgentScreenState.currentSession]
 *      │  back → clearCurrent() → EmptyState
 *      ▼
 *   FolderPicker          ← legacy [AppFolderPicker] for workspace-pinned launches
 *      │  pick app → Terminal
 *      ▼
 *   (back from EmptyState → onBackToTools)
 *
 * The state holder ([CliAgentScreenState]) owns:
 *   - sessions list (refreshed from `/cli-agent/zellij/sessions` on mount
 *     and after every successful launch/kill)
 *   - launchInFlight per-provider set
 *   - currentSession selection
 *
 * Both the empty state (T21) and the session switcher top bar (T20, wired
 * in T22) read the SAME holder so a launch fired from one surface shows a
 * spinner on the other.
 *
 * The legacy [TerminalScreen] still runs over the tmux-backed
 * [CliAgentWebSocket] today; T22+ will reroute it to a zellij-backed
 * transport using the launch response's `sessionUrl` + `token`. T21
 * deliberately preserves that backward compatibility so the screen
 * continues to work end-to-end during the migration.
 *
 * System back from EmptyState pops to the Tools menu via [onBackToTools].
 * System back from Terminal returns to EmptyState. System back from the
 * legacy FolderPicker also returns to EmptyState. TerminalScreen installs
 * its own BackHandler, so this screen's BackHandler is only active outside
 * the terminal.
 */
@Composable
fun CliAgentScreen(
    origin: String,
    operator: String,
    onBackToTools: () -> Unit,
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
    // Recreated when `operator` changes — the holder is per-operator
    // because sessions, launch state, and the current session all scope
    // to a single operator. `remember(operator, repository)` rebuilds on
    // either change.
    //
    // Errors surface as Toasts to match the existing cli_agent/ package
    // convention (see [AppFolderPicker], [TerminalScreen]).
    var state by remember { mutableStateOf<CliAgentInternalState>(CliAgentInternalState.EmptyState) }
    val screenState = remember(operator, repository) {
        CliAgentScreenState(
            scope = scope,
            repository = repository,
            operator = operator,
            onLaunched = { _ ->
                // Transition into Terminal. The session is already set as
                // current inside the holder; CliAgentScreen reads
                // screenState.currentSession to drive TerminalScreen.
                state = CliAgentInternalState.Terminal
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

    // Initial fetch + refresh when operator changes. The holder
    // intentionally does NOT auto-refresh on construct because that would
    // couple data fetching to composition timing in surprising ways;
    // explicit LaunchedEffect keeps the trigger visible at the screen.
    LaunchedEffect(operator) {
        screenState.refreshSessions()
    }

    // T20: hide the floating app chrome (operator pill / snapshot count /
    // connected indicator) while a terminal session is on screen. The
    // chrome stays visible on the EmptyState + FolderPicker branches so
    // users can still see operator + health while choosing a workspace.
    val terminalActive = state is CliAgentInternalState.Terminal
    CompositionLocalProvider(LocalShowAppChrome provides !terminalActive) {
        when (val s = state) {
            CliAgentInternalState.EmptyState -> {
                BackHandler(enabled = true) { onBackToTools() }
                Box(modifier = modifier.fillMaxSize()) {
                    CliAgentEmptyState(
                        launchInFlight = screenState.launchInFlight,
                        onLaunchProvider = { provider ->
                            // No-app launch path: triggers state-holder
                            // launch which on success transitions us to
                            // Terminal via onLaunched callback.
                            screenState.launch(provider)
                        },
                        onChooseFolderForTerminal = {
                            // Long-press fallback — switches to the legacy
                            // AppFolderPicker so the user can pick a
                            // workspace, then the picker triggers the
                            // Terminal transition with the chosen app.
                            state = CliAgentInternalState.FolderPicker
                        },
                        modifier = Modifier.fillMaxSize(),
                    )
                }
            }

            CliAgentInternalState.FolderPicker -> {
                // Legacy picker reached only via long-press on "+ Terminal".
                // Preserves the existing tmux-backed launch flow when a
                // user wants to pin a workspace.
                BackHandler(enabled = true) {
                    state = CliAgentInternalState.EmptyState
                }
                AppFolderPicker(
                    repository = repository,
                    operator = operator,
                    selectedProvider = selectedProvider,
                    onProviderSelected = { p ->
                        scope.launch { store.setCliAgentProvider(p.slug) }
                    },
                    onAppSelected = { slug, name ->
                        // Picker chose an app — transition into Terminal
                        // via the legacy (tmux) appSlug/appName path. The
                        // currentSession holder field stays null for this
                        // legacy path because TerminalScreen identifies
                        // its session via (operator, provider, appSlug)
                        // rather than a ZellijSessionRow.
                        state = CliAgentInternalState.LegacyTerminal(
                            appSlug = slug,
                            appName = name,
                            provider = selectedProvider.slug,
                        )
                    },
                    modifier = modifier,
                )
            }

            CliAgentInternalState.Terminal -> {
                // T21 transition target for zellij launches. Today the
                // TerminalScreen still runs the legacy tmux WebSocket
                // (we MUST NOT modify TerminalScreen per the T21 brief),
                // so we pass the launched session's provider and an empty
                // appSlug — matching the "+ Terminal" no-app contract.
                // T22 will swap in a zellij-backed transport that uses
                // the launch response's sessionUrl + token.
                val cur = screenState.currentSession
                // Defensive: if currentSession is/becomes null while we're
                // in the Terminal branch (e.g. a kill races with rendering),
                // bounce back to EmptyState. Keying the effect on `cur`
                // ensures it re-fires whenever currentSession transitions
                // to null — not just on first composition entry.
                LaunchedEffect(cur) {
                    if (cur == null) {
                        state = CliAgentInternalState.EmptyState
                    }
                }
                if (cur != null) {
                    TerminalScreen(
                        api = api,
                        operator = operator,
                        appSlug = cur.app ?: "",
                        appName = cur.app?.takeIf { it.isNotBlank() } ?: "Terminal",
                        provider = cur.provider,
                        onBack = {
                            screenState.clearCurrent()
                            state = CliAgentInternalState.EmptyState
                        },
                        modifier = modifier,
                    )
                }
            }

            is CliAgentInternalState.LegacyTerminal -> {
                // Backwards-compat branch for AppFolderPicker-driven launches.
                // Same TerminalScreen, but the (appSlug, appName, provider)
                // tuple is sourced from the picker rather than a ZellijSessionRow.
                TerminalScreen(
                    api = api,
                    operator = operator,
                    appSlug = s.appSlug,
                    appName = s.appName,
                    provider = s.provider,
                    onBack = { state = CliAgentInternalState.EmptyState },
                    modifier = modifier,
                )
            }
        }
    }
}

private sealed class CliAgentInternalState {
    /** No active session — show empty-state launch UI. */
    data object EmptyState : CliAgentInternalState()

    /** Long-press fallback — legacy app/folder picker. */
    data object FolderPicker : CliAgentInternalState()

    /** Zellij-launched terminal session driven by [CliAgentScreenState.currentSession]. */
    data object Terminal : CliAgentInternalState()

    /** Picker-driven legacy terminal (tmux path with explicit app pick). */
    data class LegacyTerminal(
        val appSlug: String,
        val appName: String,
        val provider: String,
    ) : CliAgentInternalState()
}
