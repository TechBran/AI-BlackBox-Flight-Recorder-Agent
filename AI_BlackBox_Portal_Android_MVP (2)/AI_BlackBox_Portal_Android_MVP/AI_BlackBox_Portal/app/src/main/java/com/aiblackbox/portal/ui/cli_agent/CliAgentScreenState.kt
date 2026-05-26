package com.aiblackbox.portal.ui.cli_agent

import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.setValue
import com.aiblackbox.portal.data.model.ZellijSession
import com.aiblackbox.portal.data.model.ZellijSessionRow
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.launch
import java.io.IOException

/**
 * Phase 4 / T21 — composition-scoped state holder for [CliAgentScreen].
 *
 * Holds three pieces of state that BOTH the [SessionSwitcherTopBar] (T20)
 * AND [CliAgentEmptyState] (T21) read:
 *   - [sessions] — the live list of this operator's zellij sessions.
 *   - [launchInFlight] — set of provider slugs whose launch is in flight,
 *     enabling per-provider spinners so independent launches don't share
 *     a single boolean.
 *   - [currentSession] — which session the user is "looking at" (drives
 *     the top bar label and which session [TerminalScreen] would render
 *     once T22 wires the zellij-backed terminal).
 *
 * **Single source of truth.** Top bar AND empty state must read this same
 * holder's [launchInFlight] / [sessions] / [currentSession] — they do NOT
 * keep their own copies. That's why the holder is screen-scoped (created
 * in `CliAgentScreen` via `remember { }`) and passed by reference.
 *
 * **State holder approach.** Chose a plain `remember { }` holder class
 * over an `AndroidViewModel` because:
 *   1. The codebase mixes both styles — picker-style screens use inline
 *      `remember + mutableStateOf` (see existing [CliAgentScreen],
 *      [AppFolderPicker]); list/chat-style screens use ViewModels
 *      (see [com.aiblackbox.portal.ui.cron.CronViewModel],
 *      [com.aiblackbox.portal.ui.voice.VoiceScreen]). T21 sits closer to
 *      the picker family.
 *   2. The state is server-refreshable — a [LaunchedEffect] in
 *      [CliAgentScreen] re-fetches [sessions] on first composition and
 *      after every successful launch/kill. Rotation-induced state loss
 *      is recoverable (per brief: "sessions list refresh on rotation is
 *      acceptable").
 *   3. No process-death persistence requirement.
 *   4. Avoids the Hilt / VM-factory boilerplate the rest of the
 *      `cli_agent/` package deliberately avoids.
 *
 * **Configuration-change caveat.** A launch in flight when the device
 * rotates will lose its spinner because the holder is rebuilt on config
 * change. The newly-launched session DOES reach the server though, so
 * the post-rotation `LaunchedEffect` re-fetch surfaces it correctly.
 * Promoting this holder to an `AndroidViewModel` is the documented
 * upgrade path if T23 device-QA flags the missing spinner as a UX
 * regression. Brief acceptance bar: "in-flight launch surviving rotation
 * is the bar" — we ship it as-is and let device QA decide.
 *
 * **Threading.** All mutators run on the composition thread (Compose
 * snapshots are thread-safe), via [scope] which is the caller's
 * `rememberCoroutineScope()`. Repository calls happen inside `launch`
 * blocks so they don't block the UI.
 */
internal class CliAgentScreenState(
    private val scope: CoroutineScope,
    private val repository: CliAgentSessionRepository,
    /**
     * Operator currently driving the screen. Passed in (not stored as
     * mutable) because operator switching is a screen-level decision —
     * the holder is recreated when operator changes via `remember(operator)`.
     */
    private val operator: String,
    /**
     * Notified when a launch transitions the screen into the Terminal
     * state. The caller decides what to render for that state (today:
     * existing TerminalScreen via the legacy tmux WebSocket; T22+: a
     * zellij-backed terminal using `session.sessionUrl` + `session.token`).
     */
    private val onLaunched: (ZellijSession) -> Unit = {},
    /**
     * Surface a user-facing error when a launch / kill / refresh fails.
     * Caller wires this to a Toast (matches the cli_agent/ package
     * convention used in [AppFolderPicker]). Reason is a short
     * one-line message suitable for a Toast.
     */
    private val onError: (action: String, reason: String) -> Unit = { _, _ -> },
) {
    var sessions: List<ZellijSessionRow> by mutableStateOf(emptyList())
        private set

    /**
     * Per-provider set so two independent launches each get their own
     * spinner. `Set<String>` (not `Map<String, Job>`) because the UI only
     * needs membership; cancellation is bound to [scope] and happens
     * implicitly when the screen leaves composition.
     */
    var launchInFlight: Set<String> by mutableStateOf(emptySet())
        private set

    var currentSession: ZellijSessionRow? by mutableStateOf(null)
        private set

    /**
     * Map of session-name → full [ZellijSession] (with `token` + `sessionUrl`)
     * for sessions that THIS holder freshly launched in this composition. Used
     * by the screen to look up transport credentials when the user picks a
     * row from the switcher — the `ZellijSessionRow` returned from
     * `GET /sessions` deliberately omits token/URL (audit I7: mint-per-launch),
     * so we keep our own breadcrumb trail per holder instance.
     *
     * **Lifetime.** Cleared when the holder is rebuilt (operator switch,
     * config change). That's intentional — the token is transient and
     * server-side state is reachable via `refreshSessions()`. If a user
     * rotates the device with a session active, they'll need to kill +
     * relaunch to reattach (T23 device-QA can flag this if it bites).
     *
     * **Why state-map, not MutableState<Map>:** the keys-by-name shape +
     * sparse update pattern (one entry per launch) doesn't need full
     * recomposition on every put; consumers (CliAgentScreen) look up by
     * name on demand inside event handlers.
     */
    private val liveSessionsByName: MutableMap<String, ZellijSession> = mutableMapOf()

    /** Look up a freshly-launched session by name, or null if not held. */
    fun liveSessionFor(name: String): ZellijSession? = liveSessionsByName[name]

    /** True while the initial sessions fetch is in flight. */
    var isInitialLoad: Boolean by mutableStateOf(true)
        private set

    /**
     * Re-fetch sessions from the orchestrator. Idempotent — failures are
     * surfaced via [onError] and leave [sessions] unchanged so a transient
     * network blip doesn't blank the top bar.
     */
    fun refreshSessions() {
        scope.launch {
            try {
                sessions = repository.listZellijSessions(operator)
                // If currentSession was deleted server-side, clear it so
                // the top bar drops back to "No session" rather than
                // showing a phantom row.
                val cur = currentSession
                if (cur != null && sessions.none { it.name == cur.name }) {
                    currentSession = null
                }
            } catch (e: IOException) {
                onError("refresh", e.message ?: "Couldn't refresh sessions")
            } finally {
                isInitialLoad = false
            }
        }
    }

    /**
     * Launch a fresh zellij session for [provider]. Adds to
     * [launchInFlight] before the call, **always** removes on completion
     * (try/finally, never a bare try) so a thrown launch never leaves a
     * stuck spinner. On success: appends to [sessions], sets as
     * [currentSession], invokes [onLaunched] so the caller can transition
     * into the Terminal state.
     *
     * @param provider one of [com.aiblackbox.portal.data.model.ZELLIJ_PROVIDER_SLUGS].
     * @param app optional workspace pin (basename of the Apps/ subdir).
     */
    fun launch(provider: String, app: String? = null) {
        // No-op guard: don't queue a duplicate launch for a provider
        // that's already mid-launch. The UI disables the button via
        // LaunchButton.isLoading, but a fast double-tap before
        // recomposition could otherwise race.
        if (provider in launchInFlight) return

        launchInFlight = launchInFlight + provider
        scope.launch {
            try {
                val session: ZellijSession = repository.launchZellijSession(
                    operator = operator,
                    provider = provider,
                    app = app,
                )
                // Synthesise a ZellijSessionRow from the launch response so
                // we can seed the top bar / list immediately without
                // waiting on a full refresh round-trip. The next refresh
                // will reconcile if the backend disagrees.
                val row = ZellijSessionRow(
                    name = session.name,
                    provider = session.provider,
                    app = session.app,
                    createdAt = session.createdAt,
                    expiresAt = session.expiresAt,
                )
                sessions = (sessions + row).distinctBy { it.name }
                currentSession = row
                // Stash the live session (with token + sessionUrl) so the
                // screen can construct the WS client on selectSession too,
                // not only on the just-fired launch callback.
                liveSessionsByName[session.name] = session
                onLaunched(session)
                // Schedule a follow-up refresh to pick up server-side
                // fields the launch response didn't carry (createdAt etc).
                refreshSessions()
            } catch (e: IllegalArgumentException) {
                // Unknown provider slug — should never happen via UI but
                // is plausible if a caller passes a typo'd literal.
                onError("launch", e.message ?: "Unknown provider")
            } catch (e: IOException) {
                onError("launch", e.message ?: "Couldn't launch $provider")
            } finally {
                launchInFlight = launchInFlight - provider
            }
        }
    }

    /**
     * Kill a session by name. On success: removes from [sessions] and, if
     * it was the [currentSession], advances to the next available session
     * in the list (falls back to null if no sessions remain). The "advance"
     * behavior is a deliberate UX choice — killing one session shouldn't
     * leave the user stranded on "No session" when they have other sessions
     * open. Top bar drops back to whichever session is next, or "No session"
     * only when the killed session was the last one.
     */
    fun kill(row: ZellijSessionRow) {
        scope.launch {
            try {
                repository.killZellijSession(operator, row.name)
                sessions = sessions.filterNot { it.name == row.name }
                // Drop the live session breadcrumb — the token's revoked
                // server-side and the session no longer exists.
                liveSessionsByName.remove(row.name)
                if (currentSession?.name == row.name) {
                    currentSession = sessions.firstOrNull()
                }
            } catch (e: IOException) {
                onError("kill", e.message ?: "Couldn't kill session")
            }
        }
    }

    /**
     * Select a session as current. Pure state mutation — the caller is
     * responsible for transitioning the screen into Terminal state.
     */
    fun selectSession(row: ZellijSessionRow) {
        currentSession = row
    }

    /**
     * Clear the current session selection. Used by the back navigation
     * out of the terminal — the session remains in [sessions] so the user
     * can reattach via the switcher.
     */
    fun clearCurrent() {
        currentSession = null
    }
}
