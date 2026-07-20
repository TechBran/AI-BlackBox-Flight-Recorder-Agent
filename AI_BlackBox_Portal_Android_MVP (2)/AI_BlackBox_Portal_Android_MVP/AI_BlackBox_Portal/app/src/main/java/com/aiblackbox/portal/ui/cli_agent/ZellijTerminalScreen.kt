package com.aiblackbox.portal.ui.cli_agent

// ZellijTerminalScreen — T22 sibling of [TerminalScreen] that drives the
// Termux TerminalView from [ZellijWebSocketClient] (zellij-web two-socket
// protocol) instead of [CliAgentWebSocket] (tmux-backed REST WS).
//
// Why a separate Composable instead of extending TerminalScreen:
//   • TerminalScreen (~700 lines) embeds tmux-specific lifecycle: bracketed
//     paste via `ws.sendPaste()`, single-binary-frame transport, exponential
//     reconnect inside the client itself, etc. Mid-flight refactor under
//     the T22 ship-it-together pressure is too risky.
//   • The legacy AppFolderPicker → TerminalScreen flow (E22-era tmux) must
//     keep working unchanged per the T22 brief. Cleanest isolation = two
//     siblings, both consuming the same Termux PTY-bridge trick.
//
// What's reused: ExtraKeysBar, CliMicButton, the Termux TerminalView +
// TerminalSession bridging trick (local sleep child, append bytes to the
// emulator directly), the ZellijBannerKind/ReconnectBanner visual contract.
//
// What differs from TerminalScreen:
//   • Client: ZellijWebSocketClient(origin, sessionName, sessionToken, scope)
//     instead of CliAgentWebSocket(baseUrl, sessionId, params, callbacks).
//   • Connection: lifecycle ownership lives in the process-scoped
//     [TerminalSessionManager] (Phase 1, 2026-06-22). Entering binds (reuse
//     live client or connect); leaving DETACHES the renderer but keeps the
//     socket alive. Only the explicit X button (manager.kill + DELETE) closes.
//   • Bytes: client.onBytes(bytes, length) feeds straight into
//     TerminalEmulator.append(bytes, length).
//   • Paste from Whisper: zellij protocol carries paste as bracketed-paste
//     bytes inline (no separate paste frame). We wrap the transcript in
//     ESC[200~…ESC[201~ ourselves and ship via sendBytes.

import android.content.Context
import android.util.Log
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.inputmethod.InputMethodManager
import androidx.activity.compose.BackHandler
import androidx.compose.animation.core.AnimationState
import androidx.compose.animation.core.DecayAnimationSpec
import androidx.compose.animation.core.animateDecay
import androidx.compose.animation.splineBasedDecay
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.awaitEachGesture
import androidx.compose.foundation.gestures.awaitFirstDown
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.runtime.withFrameNanos
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.changedToUpIgnoreConsumed
import androidx.compose.ui.input.pointer.pointerInput
import androidx.compose.ui.input.pointer.util.VelocityTracker
import androidx.compose.ui.input.pointer.util.addPointerInputChange
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.api.ZellijWebSocketClient
import com.aiblackbox.portal.data.model.ZellijSession
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral500
import com.termux.terminal.TerminalEmulator
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.termux.view.TerminalView
import com.termux.view.TerminalViewClient
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Job
import kotlinx.coroutines.launch

private const val TAG = "ZellijTerminalScreen"

private const val DEFAULT_COLS = 80
private const val DEFAULT_ROWS = 24
private const val TRANSCRIPT_ROWS = 2000

/**
 * Trailing-debounce window for pushing a new grid size to zellij. Rotation and
 * IME animations emit a BURST of intermediate cols/rows; the [LaunchedEffect]
 * keyed on them is cancelled + relaunched on each change, so only the FINAL
 * size survives this delay and reaches the (heavyweight) zellij session reflow.
 * 150ms comfortably outlasts a rotation/IME animation without perceptible lag.
 */
private const val RESIZE_DEBOUNCE_MS = 150L

/**
 * Zellij-backed terminal Composable. Hosts a Termux [TerminalView] inside
 * [AndroidView], proxies bytes between the emulator and a freshly-minted
 * [ZellijWebSocketClient], and shows an [ExtraKeysBar] + [CliMicButton]
 * at the bottom.
 *
 * Lifecycle (Phase 1, 2026-06-22): the [ZellijWebSocketClient] is owned by
 * the process-scoped [TerminalSessionManager], NOT this composition. Entering
 * binds via [TerminalSessionManager.getOrConnect] (reusing a live client if
 * one exists for this session name, else connecting a fresh one); leaving
 * composition calls [TerminalSessionManager.detach] which stops forwarding
 * bytes to the dead TerminalView but KEEPS the socket alive. The token
 * carried in [session] is transient (audit I7); after the WS handshake the
 * server holds session state.
 *
 * Back behavior: detach only — the zellij session survives both in the
 * orchestrator AND on the client side (manager keeps the socket). Killing
 * happens ONLY through [SessionSwitcherTopBar]'s X (manager.kill + backend
 * DELETE).
 */
@Composable
fun ZellijTerminalScreen(
    api: BlackBoxApi,
    operator: String,
    session: ZellijSession,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val density = LocalDensity.current

    // --- Connection state surfaced to the banner ---------------------------
    var bannerText by remember { mutableStateOf<String?>("Connecting…") }
    var bannerKind by remember { mutableStateOf(ZellijBannerKind.Info) }

    // --- Termux view / session references -----------------------------------
    var terminalView by remember { mutableStateOf<TerminalView?>(null) }
    var terminalSession by remember { mutableStateOf<TerminalSession?>(null) }

    // --- Grid dimensions; pushed via sendResize whenever they change --------
    var cols by remember { mutableStateOf(DEFAULT_COLS) }
    var rows by remember { mutableStateOf(DEFAULT_ROWS) }

    // Last size actually PUSHED to zellij (post-debounce). Distinct from
    // cols/rows (the live measured size): the send guard compares against this
    // so a burst that collapses back to the same grid sends nothing. Starts at
    // 0 so the first real measured size always sends.
    var lastSentCols by remember(session.name) { mutableStateOf(0) }
    var lastSentRows by remember(session.name) { mutableStateOf(0) }

    // --- ZellijWebSocketClient ownership (Phase 1: persistent sessions) ----
    //
    // The client is NO LONGER constructed/owned by this composition. Phase 1
    // (2026-06-22) hoists connection + session-handle ownership into the
    // process-lived [TerminalSessionManager] so leaving this screen DETACHES
    // the renderer (stops forwarding bytes to a dead TerminalView) but never
    // CLOSES the socket. Re-entering the same session reuses the live client
    // (no new POST /session, no new socket). Only the explicit X button
    // (-> manager.kill + backend DELETE) closes a socket.
    //
    // Origin defaults to BlackBoxApi.getBaseUrl(); ZellijWebSocketClient
    // normalises http(s)/ws(s) variants internally. The sessionName comes
    // from the launch response (passed in via [session]). webClientId is
    // auto-generated (UUID) inside the client. Phase 5 (2026-05-26): the
    // sessionToken is no longer passed — the orchestrator app-proxy injects
    // the master cookie on upstream forward, so the client opens the
    // WebSocket with no auth state of its own.
    //
    // remember(session.name) holds the manager-owned client reference stable
    // for this composition; the manager (not this remember) owns its lifetime.
    val listener = remember(session.name) {
        object : ZellijWebSocketClient.Listener {
            override fun onConnected() {
                Log.d(TAG, "ws onConnected")
                bannerText = null
            }

            override fun onBytes(bytes: ByteArray, length: Int) {
                val view = terminalView ?: return
                view.post {
                    try {
                        val sess = terminalSession ?: return@post
                        val emulator: TerminalEmulator? = sess.emulator
                        if (emulator != null) {
                            emulator.append(bytes, length)
                            view.onScreenUpdated()
                        } else {
                            Log.w(TAG, "No emulator on session — dropping $length bytes")
                        }
                    } catch (t: Throwable) {
                        Log.e(TAG, "Failed to feed bytes to emulator", t)
                    }
                }
            }

            override fun onSwitchedSession(newSessionName: String) {
                Log.d(TAG, "ws onSwitchedSession → $newSessionName")
                // Surface as info banner; the holder's currentSession label
                // updates lazily on the next refresh. No-op for the emulator.
                bannerText = "Switched to $newSessionName"
                bannerKind = ZellijBannerKind.Info
            }

            override fun onDisconnected(code: Int, reason: String, willReconnect: Boolean) {
                Log.d(TAG, "ws onDisconnected code=$code reason=$reason reconnect=$willReconnect")
                bannerText = if (willReconnect) {
                    "Reconnecting… (${reason.ifBlank { "code $code" }})"
                } else {
                    "Disconnected (${reason.ifBlank { "code $code" }})"
                }
                bannerKind = if (willReconnect) ZellijBannerKind.Warn else ZellijBannerKind.Error
            }

            override fun onError(throwable: Throwable) {
                Log.w(TAG, "ws onError", throwable)
                bannerText = "Error: ${throwable.message ?: "unknown"}"
                bannerKind = ZellijBannerKind.Error
            }
        }
    }

    // Bind to the process-lived client for this session name. Returns the
    // existing live client (re-binding this renderer) if one is held, else
    // creates + connects a new one. The client's coroutines run on the
    // manager's process scope (NOT this composition's rememberCoroutineScope)
    // so reconnect survives navigation. We re-fetch the same instance on
    // recomposition via remember(session.name).
    val client: ZellijWebSocketClient = remember(session.name) {
        TerminalSessionManager.getOrConnect(
            session = session,
            api = api,
            scope = TerminalSessionManager.scope,
            listener = listener,
        )
    }

    // --- Swipe-scroll physics (fling + gain + paced wheel emission) ---------
    //
    // One engine per screen: owns the shared px→step accumulator, the
    // post-finger-up decay-fling job, and the frame-paced SGR-wheel drainer.
    // All physics/pacing DECISIONS are pure functions/classes below
    // ([terminalRowHeightPx], [pixelsPerScrollStep], [ScrollLineAccumulator],
    // [WheelNotchPacer], [coalescedArrowCount], [shouldFling]) so unit tests
    // pin the math; the engine is only plumbing. The providers close over
    // Compose MutableState and are re-read on EVERY tick — the withFrameNanos
    // drainer therefore never acts on a stale capture (the ember-backdrop
    // rememberUpdatedState lesson, solved here by reading live state instead
    // of capturing values). Keyed on session.name to mirror [client]'s key.
    val scrollScope = rememberCoroutineScope()
    val scrollEngine = remember(session.name) {
        ZellijScrollEngine(
            scope = scrollScope,
            viewProvider = { terminalView },
            clientProvider = { client },
        )
    }
    DisposableEffect(scrollEngine) {
        onDispose {
            // Leaving composition: kill any live fling + queued wheel backlog.
            // (scrollScope is also cancelled by Compose, so the drainer dies
            // even mid-frame; this just makes the stop explicit + immediate.)
            scrollEngine.stopAll()
        }
    }

    // --- Esc RELAY (2026-07-11) ---------------------------------------------
    // zellij-web's terminal-WS input parser holds a BARE ESC frame forever
    // waiting for an escape-sequence continuation (no ESC timeout on the web
    // path) — a lone 0x1b frame NEVER resolves to the Esc key, so Claude Code
    // menus could not be dismissed. Typing and complete sequences (arrows)
    // are unaffected — exactly the observed symptom. Every key path (Esc
    // button, hardware KEYCODE_ESCAPE, Ctrl+[) therefore routes a lone-ESC
    // through the orchestrator's `zellij action write 27` injection (POST
    // /cli-agent/zellij/send-key — live-validated on a real stuck session);
    // anything else keeps the normal WS byte path. On relay failure the raw
    // byte is sent as a fallback (better than swallowing the keypress).
    val sendKeyInput: (ByteArray) -> Unit = { bytes ->
        if (bytes.size == 1 && bytes[0] == 0x1b.toByte()) {
            scrollScope.launch {
                try {
                    val enc = java.net.URLEncoder.encode(operator, "UTF-8")
                    api.post(
                        "/cli-agent/zellij/send-key?op=$enc",
                        org.json.JSONObject()
                            .put("session", session.name)
                            .put("bytes", org.json.JSONArray().put(27))
                            .toString()
                    )
                } catch (e: Exception) {
                    client.sendBytes(bytes)
                }
            }
        } else {
            client.sendBytes(bytes)
        }
    }

    // --- Detach (NOT close) on dispose --------------------------------------
    //
    // Leaving composition (back nav, session swap) must DETACH only: stop
    // forwarding bytes to this soon-to-be-dead TerminalView while keeping the
    // socket alive in the manager. The session survives in the orchestrator
    // AND on the client side; re-entry rebinds. NEVER call client.close()
    // here — that is the explicit-kill-only path (the X button).
    DisposableEffect(client) {
        onDispose {
            try {
                TerminalSessionManager.detach(
                    name = session.name,
                    cols = cols,
                    rows = rows,
                )
            } catch (_: Throwable) {
            }
        }
    }

    // --- System back: detach only -------------------------------------------
    BackHandler(enabled = true) {
        onBack()
    }

    // --- Push resize whenever cols/rows change (trailing debounce) ----------
    //
    // Rotation and IME animations emit a BURST of intermediate cols/rows.
    // Because this LaunchedEffect is keyed on (cols, rows) it is cancelled +
    // relaunched on every change, so the delay below collapses that burst to
    // just the FINAL size — each survivor is one heavyweight zellij session
    // reflow, so we want exactly one per rotation, not one per animation frame.
    LaunchedEffect(cols, rows) {
        kotlinx.coroutines.delay(RESIZE_DEBOUNCE_MS)

        // Guard (pure, unit-tested in ZellijWebSocketClientTest): never push a
        // non-positive grid, and skip a burst that collapsed back to the size
        // we already sent.
        if (!ZellijWebSocketClient.shouldSendResize(cols, rows, lastSentCols, lastSentRows)) {
            return@LaunchedEffect
        }

        Log.d(TAG, "Resize → ${cols}x${rows} (debounced)")
        try {
            terminalSession?.updateSize(cols, rows)
        } catch (t: Throwable) {
            Log.w(TAG, "session.updateSize failed", t)
        }
        client.sendResize(cols = cols, rows = rows)
        lastSentCols = cols
        lastSentRows = rows

        // Unconditional repaint AFTER the debounced resize for a NEW grid. A
        // rotation that races zellij's redraw otherwise leaves the OLD grid
        // painted (the "rotate twice to fix it" bug); and a reattach's fresh,
        // empty TerminalView needs the existing screen redrawn too. zellij
        // repaints its whole frame on a TerminalResize, so requestRepaint()
        // (a 1-row toggle) guarantees a full redraw for the current size.
        client.requestRepaint()
    }

    // --- Compose UI ---------------------------------------------------------
    //
    // Insets ownership (Task 5, single-owner rule): this screen is NO LONGER
    // the top-inset owner. The Scaffold's SessionSwitcherTopBar (in
    // CliAgentScreen) is visible over the terminal and already consumes the
    // status-bar inset + its own 40dp height; the Terminal branch passes only
    // that top inset down as padding. So we DROP statusBarsPadding() here to
    // avoid double top padding. This Column remains the SOLE owner of the
    // BOTTOM (nav-bar) inset and the IME inset via navigationBarsPadding() +
    // imePadding(), so the emulator claims full width and full height minus the
    // real system bars + keyboard, with no wasted border.
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(BbxBlack)
            .navigationBarsPadding()
            .imePadding(),
    ) {
        // --- Terminal region (fills all vertical space; banner overlays it) ----
        //
        // Task 5: the reconnect banner is a top-aligned OVERLAY inside this
        // weighted Box, NOT a Column sibling. As a sibling it consumed layout
        // height, shrinking the emulator every time it appeared/disappeared
        // while (re)connecting — a resize churn (exactly what Task 6 addresses).
        // As an overlay the emulator keeps its full height regardless of banner
        // visibility; the banner just draws transiently over the top rows.
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = true),
        ) {
        // --- Terminal surface --------------------------------------------------
        //
        // The outer Box wraps the AndroidView<TerminalView> with a Compose
        // pointerInput layer that arbitrates EVERY pointer gesture over the
        // terminal BEFORE it can reach the Termux TerminalView.
        //
        // T23 device QA (2026-05-26) + Task 7 (2026-07-03): when claude turns
        // on mouse-tracking mode (CSI ?1000h/?1003h/?1006h) the Termux
        // TerminalView encodes EVERY touch it receives — including a
        // slightly-draggy tap — as an SGR mouse report ("<65;44;17M") that
        // renders into the prompt then vanishes on the next redraw (the
        // "phantom characters on tap" bug). detectVerticalDragGestures only
        // consumed drags PAST touch-slop, so a sub-slop draggy tap still leaked
        // through to the TerminalView's encoder. The fix: while mouse tracking
        // is active this layer OWNS the whole gesture — a tap becomes focus +
        // keyboard ONLY (zero bytes), a deliberate vertical drag becomes a
        // clean SGR wheel sequence, everything else is swallowed. Nothing raw
        // ever reaches the TerminalView, so no mouse report can leak.
        //
        // Live state, never cached: the scroll branch (see [scrollBranchFor])
        // is chosen from the emulator flags read AT gesture time on every
        // scroll, so a plain-terminal session that manually launches claude
        // scrolls exactly like a claude-launched one — the branch keys off live
        // emulator state, never the provider the session was launched as.
        //
        // Native scroll feel (2026-07-09): interception amputated the
        // TerminalView's built-in fling (the child sees ACTION_CANCEL), so we
        // reimplement the physics OURSELVES on this side of the ownership
        // boundary: VelocityTracker in the drag loop → splineBasedDecay fling
        // on finger-up ([ZellijScrollEngine]), row-height-derived gain instead
        // of the hardcoded 20f, per-line arrows instead of page-per-notch for
        // PAGE, and frame-paced rate-capped SGR wheel emission. Ownership /
        // consumption rules are UNCHANGED — the fling never injects pointer
        // events, so nothing new can leak to the TerminalView.
        Box(
            modifier = Modifier
                .fillMaxSize()
                .background(BbxBlack)
                .pointerInput(Unit) {
                    // Post-finger-up fling physics: the standard Android
                    // scroll-friction curve. Built once per pointer-input
                    // scope with the live density (PointerInputScope IS a
                    // Density), consumed by [ZellijScrollEngine.startFling].
                    val decaySpec: DecayAnimationSpec<Float> = splineBasedDecay(this)
                    val slop = viewConfiguration.touchSlop
                    awaitEachGesture {
                        val down = awaitFirstDown(requireUnconsumed = false)
                        // Touch-to-stop: a finger on the glass halts any live
                        // fling and drops the queued wheel backlog immediately
                        // (native scroll feel; also guarantees a fling can
                        // never fight the gesture that follows).
                        scrollEngine.stopAll()
                        // LIVE mouse-tracking flag, read at gesture START (never
                        // cached across gestures/sessions). When active we own
                        // every event so the TerminalView never sees a touch to
                        // encode → no phantom mouse report can leak. Consume the
                        // down up-front to claim the gesture from the interop
                        // child.
                        val mouseTracking =
                            terminalView?.mEmulator?.isMouseTrackingActive == true
                        if (mouseTracking) down.consume()

                        // Finger velocity for the fling handoff. Tracking a
                        // pointer NEVER changes event consumption — the
                        // interception/ownership rules below are byte-identical
                        // to the pre-fling code (commit 23aaadf's airtightness).
                        val velocityTracker = VelocityTracker()
                        velocityTracker.addPointerInputChange(down)

                        var pointerId = down.id
                        var totalY = 0f
                        var totalX = 0f
                        var isDrag = false

                        while (true) {
                            val event = awaitPointerEvent()
                            val change = event.changes.firstOrNull { it.id == pointerId }
                                ?: event.changes.firstOrNull()
                                ?: continue
                            if (change.id != pointerId) {
                                // Pointer swap (tracked finger left, fallback
                                // retargeted another): the position jump
                                // between two fingers is not motion — reset so
                                // the tracker can't synthesize a violent fling
                                // from it.
                                velocityTracker.resetTracking()
                            }
                            pointerId = change.id
                            velocityTracker.addPointerInputChange(change)
                            val dy = change.position.y - change.previousPosition.y
                            val dx = change.position.x - change.previousPosition.x
                            totalY += dy
                            totalX += dx

                            if (change.changedToUpIgnoreConsumed()) {
                                // Gesture ended without ever becoming a drag → a
                                // tap. Under mouse tracking we do focus +
                                // keyboard ourselves and swallow it (zero bytes);
                                // otherwise let it fall through to the
                                // TerminalView, whose onSingleTapUp does the same
                                // focus/keyboard (preserving pre-Task-7 behavior).
                                if (!isDrag && mouseTracking) {
                                    focusAndShowKeyboard(terminalView)
                                    change.consume()
                                }
                                if (isDrag) {
                                    // Finger left the glass mid-scroll: hand the
                                    // residual velocity to the decay fling. The
                                    // fling injects ZERO pointer events — it
                                    // feeds pixel deltas into the SAME
                                    // accumulator → deliver path as the finger
                                    // (branch re-resolved live per frame), so
                                    // nothing new can reach the TerminalView
                                    // while it runs.
                                    val velocityY = velocityTracker.calculateVelocity().y
                                    if (shouldFling(velocityY)) {
                                        scrollEngine.startFling(velocityY, decaySpec)
                                    }
                                }
                                break
                            }

                            // Promote to a drag once vertical travel clears slop
                            // and dominates horizontal (a real scroll, not a
                            // sideways smudge).
                            if (!isDrag &&
                                kotlin.math.abs(totalY) > slop &&
                                kotlin.math.abs(totalY) >= kotlin.math.abs(totalX)
                            ) {
                                isDrag = true
                                scrollEngine.resetAccumulator()
                            }

                            if (isDrag) {
                                // Gain + branch resolved LIVE inside the engine:
                                // px-per-step comes from the real row height
                                // (view.height / emu.mRows, 20f pre-layout
                                // fallback), so one row of finger travel moves
                                // one row of content — the old hardcoded 20f
                                // was ~2x too hot on real row heights (~40px).
                                scrollEngine.feedPixels(dy)
                                // A drag is always ours (both modes) — consume so
                                // the TerminalView can't also encode it.
                                change.consume()
                            } else if (mouseTracking) {
                                // Pre-drag movement under mouse tracking: swallow
                                // so a draggy-tap never reaches the encoder.
                                change.consume()
                            }
                        }
                    }
                }
                .onSizeChanged { _ ->
                    val v = terminalView ?: return@onSizeChanged
                    v.post {
                        val emu = v.mEmulator ?: return@post
                        val nc = emu.mColumns
                        val nr = emu.mRows
                        if (nc > 0 && nr > 0 &&
                            (nc != cols || nr != rows)) {
                            cols = nc
                            rows = nr
                            // Keep the client's measured-size cache current
                            // SYNCHRONOUSLY (cheap, no wire I/O) so a reconnect
                            // / QueryTerminalSize reply reflects the latest grid
                            // even before the debounced resize fires. See
                            // ZellijWebSocketClient.updateMeasuredSize.
                            client.updateMeasuredSize(nc, nr)
                        }
                    }
                },
        ) {
            AndroidView(
                modifier = Modifier.fillMaxSize(),
                factory = { ctx ->
                    val view = TerminalView(ctx, /* attrs = */ null)
                    view.setTextSize(with(density) { 14.sp.toPx() }.toInt())
                    view.isFocusable = true
                    view.isFocusableInTouchMode = true

                    val sessionClient = object : TerminalSessionClient {
                        override fun onTextChanged(changedSession: TerminalSession) {
                            view.onScreenUpdated()
                        }
                        override fun onTitleChanged(changedSession: TerminalSession) { /* no-op */ }
                        override fun onSessionFinished(finishedSession: TerminalSession) { /* no-op */ }
                        override fun onCopyTextToClipboard(session: TerminalSession, text: String) { /* TODO */ }
                        override fun onPasteTextFromClipboard(session: TerminalSession) { /* TODO */ }
                        override fun onBell(session: TerminalSession) { /* no-op */ }
                        override fun onColorsChanged(session: TerminalSession) { view.onScreenUpdated() }
                        override fun onTerminalCursorStateChange(state: Boolean) { /* no-op */ }
                        override fun getTerminalCursorStyle(): Int = TerminalEmulator.DEFAULT_TERMINAL_CURSOR_STYLE
                        override fun logError(tag: String, message: String) { Log.e(tag, message) }
                        override fun logWarn(tag: String, message: String) { Log.w(tag, message) }
                        override fun logInfo(tag: String, message: String) { Log.i(tag, message) }
                        override fun logDebug(tag: String, message: String) { Log.d(tag, message) }
                        override fun logVerbose(tag: String, message: String) { Log.v(tag, message) }
                        override fun logStackTraceWithMessage(tag: String, message: String, e: Exception) {
                            Log.e(tag, message, e)
                        }
                        override fun logStackTrace(tag: String, e: Exception) { Log.e(tag, "stack", e) }
                    }

                    val viewClient = object : TerminalViewClient {
                        override fun onScale(scale: Float): Float = scale
                        override fun shouldBackButtonBeMappedToEscape(): Boolean = false
                        override fun shouldEnforceCharBasedInput(): Boolean = false
                        override fun shouldUseCtrlSpaceWorkaround(): Boolean = false
                        override fun isTerminalViewSelected(): Boolean = true
                        override fun copyModeChanged(copyMode: Boolean) { /* no-op */ }

                        override fun onSingleTapUp(e: MotionEvent) {
                            view.requestFocus()
                            val imm = view.context
                                .getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager
                            imm?.showSoftInput(view, InputMethodManager.SHOW_IMPLICIT)
                        }

                        override fun onKeyDown(
                            keyCode: Int,
                            e: KeyEvent,
                            session: TerminalSession,
                        ): Boolean {
                            // Special keys routed via WS; see TerminalScreen for rationale.
                            val bytes: ByteArray? = when (keyCode) {
                                KeyEvent.KEYCODE_ENTER -> byteArrayOf(0x0d)
                                KeyEvent.KEYCODE_DEL -> byteArrayOf(0x7f)
                                KeyEvent.KEYCODE_FORWARD_DEL ->
                                    byteArrayOf(0x1b, '['.code.toByte(), '3'.code.toByte(), '~'.code.toByte())
                                KeyEvent.KEYCODE_TAB -> byteArrayOf(0x09)
                                KeyEvent.KEYCODE_ESCAPE -> byteArrayOf(0x1b)
                                KeyEvent.KEYCODE_DPAD_UP ->
                                    byteArrayOf(0x1b, '['.code.toByte(), 'A'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_DOWN ->
                                    byteArrayOf(0x1b, '['.code.toByte(), 'B'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_LEFT ->
                                    byteArrayOf(0x1b, '['.code.toByte(), 'D'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_RIGHT ->
                                    byteArrayOf(0x1b, '['.code.toByte(), 'C'.code.toByte())
                                else -> null
                            }
                            if (bytes != null) {
                                // Programmatic view reset → any live fling (and
                                // its queued wheel backlog) must die with it.
                                scrollEngine.stopAll()
                                view.setTopRow(0)
                                // Hardware KEYCODE_ESCAPE emits a lone 0x1b —
                                // same relay as the Esc button (see sendKeyInput).
                                sendKeyInput(bytes)
                                return true
                            }
                            return false
                        }

                        override fun onKeyUp(keyCode: Int, e: KeyEvent): Boolean = false
                        override fun readControlKey(): Boolean = false
                        override fun readAltKey(): Boolean = false
                        override fun readShiftKey(): Boolean = false
                        override fun readFnKey(): Boolean = false

                        override fun onCodePoint(
                            codePoint: Int,
                            ctrlDown: Boolean,
                            session: TerminalSession,
                        ): Boolean {
                            val bytes: ByteArray = if (ctrlDown && codePoint in 0x40..0x7F) {
                                byteArrayOf((codePoint and 0x1f).toByte())
                            } else if (ctrlDown && codePoint in 0x60..0x7A) {
                                byteArrayOf((codePoint and 0x1f).toByte())
                            } else {
                                String(Character.toChars(codePoint)).toByteArray(Charsets.UTF_8)
                            }
                            // Programmatic view reset → kill any live fling too.
                            scrollEngine.stopAll()
                            view.setTopRow(0)
                            // Ctrl+[ maps to a lone 0x1b — same relay as Esc
                            // (see sendKeyInput); all other input passes through.
                            sendKeyInput(bytes)
                            return true
                        }

                        override fun onLongPress(event: MotionEvent): Boolean = false
                        override fun onEmulatorSet() { Log.d(TAG, "TerminalView: emulator set") }
                        override fun logError(tag: String, message: String) { Log.e(tag, message) }
                        override fun logWarn(tag: String, message: String) { Log.w(tag, message) }
                        override fun logInfo(tag: String, message: String) { Log.i(tag, message) }
                        override fun logDebug(tag: String, message: String) { Log.d(tag, message) }
                        override fun logVerbose(tag: String, message: String) { Log.v(tag, message) }
                        override fun logStackTraceWithMessage(tag: String, message: String, e: Exception) {
                            Log.e(tag, message, e)
                        }
                        override fun logStackTrace(tag: String, e: Exception) { Log.e(tag, "stack", e) }
                    }

                    view.setTerminalViewClient(viewClient)

                    // Real TerminalSession with harmless local sleep child;
                    // bytes flow through the WS, not this PTY. Same trick
                    // [TerminalScreen] uses — see its long comment for why.
                    //
                    // SCROLLBACK PERSISTENCE (2026-06-25): the TerminalSession
                    // owns the TerminalEmulator, which owns the 2000-row
                    // scrollback transcript. We hoist it into the process-lived
                    // [TerminalSessionManager] (alongside the socket) and
                    // get-or-create BY SESSION NAME, so navigating away and back
                    // REUSES the same emulator — the scrollback survives instead
                    // of being recreated empty on every mount. First launch runs
                    // the lambda below to create it; a reattach returns the
                    // persisted session and skips creation. (Reaped on the X
                    // button via TerminalSessionManager.kill → finishIfRunning.)
                    //
                    // argv convention (T23-surfaced bug, 2026-05-26):
                    // Termux TerminalSession's `args` is the FULL argv
                    // including argv[0], NOT extra args after the binary.
                    // /system/bin/sleep is a toybox symlink that routes by
                    // argv[0]; passing args=arrayOf("999999") sets
                    // argv[0]="999999" and toybox errors out with
                    // "unknown command 999999, code 127". Pass "sleep" as
                    // argv[0] so toybox dispatches correctly + "999999"
                    // becomes argv[1] (the duration). The legacy
                    // TerminalScreen had the same latent bug but tmux
                    // bytes arrived fast enough to overwrite the error
                    // before the user saw it; ZellijWebSocketClient's
                    // auth pre-flight + version probe is slower so the
                    // error stayed visible.
                    val sess = TerminalSessionManager.getOrCreateTerminalSession(session.name) {
                        TerminalSession(
                            /* shellPath      = */ "/system/bin/sleep",
                            /* cwd            = */ "/",
                            /* args           = */ arrayOf("sleep", "999999"),
                            /* env            = */ arrayOf<String>(),
                            /* transcriptRows = */ TRANSCRIPT_ROWS,
                            /* client         = */ sessionClient,
                        )
                    }

                    // Rebind the emulator's session-client to THIS view's client.
                    // First create: same instance. Reattach: swaps the previous
                    // (dead-view) client out — releasing that view, so persisting
                    // the session can't leak the old TerminalView — so emulator
                    // callbacks (onTextChanged/onColorsChanged → onScreenUpdated)
                    // target the live view.
                    sess.updateTerminalSessionClient(sessionClient)

                    // attachSession re-links this fresh view to the (possibly
                    // persisted) emulator. On reuse it pulls in the existing
                    // emulator with its transcript intact, so the user can scroll
                    // back through history immediately on return.
                    view.attachSession(sess)

                    terminalView = view
                    // Same instance the view just attached to: the composition and
                    // the view feed ONE emulator (onBytes appends to
                    // terminalSession.emulator = the persisted emulator). No
                    // split-brain between what's fed and what's shown.
                    terminalSession = sess
                    view
                },
                update = { view ->
                    val emu = view.mEmulator
                    val newCols: Int = emu?.mColumns ?: cols
                    val newRows: Int = emu?.mRows ?: rows
                    if (newCols != cols || newRows != rows) {
                        cols = newCols
                        rows = newRows
                        // Mirror onSizeChanged: keep the client's measured-size
                        // cache current so reconnect/query replies reconcile.
                        client.updateMeasuredSize(newCols, newRows)
                    }
                },
            )
        }

            // --- Reconnect / status banner (overlay) ---------------------------
            //
            // Drawn LAST inside the weighted Box so it renders on top of the
            // emulator (Compose z-order = declaration order), pinned to the top
            // edge. Transient: bannerText is non-null only while connecting /
            // reconnecting / on error. Because it's an overlay it takes no part
            // in the Box's layout sizing, so showing/hiding it never resizes
            // the emulator (no reflow churn).
            val bannerLine = bannerText
            if (bannerLine != null) {
                ReconnectBanner(
                    text = bannerLine,
                    kind = bannerKind,
                    modifier = Modifier.align(Alignment.TopCenter),
                )
            }
        }

        // --- Extra-keys bar + mic ----------------------------------------------
        ExtraKeysBar(
            onKeyBytes = { bytes ->
                // Programmatic view reset → kill any live fling too.
                scrollEngine.stopAll()
                terminalView?.setTopRow(0)
                sendKeyInput(bytes)  // lone-ESC → orchestrator relay (see above)
            },
            onScrollLines = { delta ->
                // A deliberate button press takes over from any running fling
                // (and its queued wheel backlog) — never mix the two.
                scrollEngine.stopAll()
                // Route the PgUp/PgDn buttons through the SAME live-state branch
                // as the swipe path — so in a mouse-tracking TUI (claude) the
                // button sends the WHEEL instead of a bare PgUp the app ignores.
                deliverButtonScroll(terminalView, client, delta)
            },
            micSlot = {
                CliMicButton(
                    onTranscript = { transcript ->
                        // Programmatic view reset → kill any live fling too.
                        scrollEngine.stopAll()
                        terminalView?.setTopRow(0)
                        // Zellij has no separate paste control frame — wrap
                        // in bracketed-paste sequences ourselves and ship as
                        // raw bytes. ESC[200~ … ESC[201~ tells the receiving
                        // app this is a paste and not user typing.
                        client.sendBytes(buildBracketedPaste(transcript))
                    },
                    api = api,
                    operator = operator,
                )
            },
            attachSlot = {
                CliAttachButton(
                    operator = operator,
                    // DRIFT-TRACKED name, read per upload: zellij's
                    // SwitchedSession flips the client's effective session
                    // away from session.name (the static launch name) —
                    // capturing the latter would attach into the WRONG pane.
                    getSessionName = { client.effectiveSessionName() },
                    api = api,
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

// =========================================================================
// Helpers
// =========================================================================

/**
 * Which mechanism delivers a scroll gesture to the running program.
 *   - [WHEEL] : SGR mouse wheel report — the TUI has mouse tracking on.
 *   - [PAGE]  : PgUp/PgDn — an alt-buffer TUI without mouse tracking.
 *   - [LOCAL] : the emulator's own scrollback transcript — a normal shell.
 */
internal enum class ScrollBranch { WHEEL, PAGE, LOCAL }

/**
 * Choose the scroll-delivery mechanism from LIVE emulator flags read at the
 * moment of the gesture. Mouse tracking wins even inside the alt buffer (that
 * is how claude/htop expect the wheel); a no-mouse alt buffer gets PgUp/PgDn;
 * a normal-buffer shell scrolls its local transcript. Pure + [internal] so a
 * unit test can pin the ordering, and so the branch can NEVER key off the
 * provider a session was launched as — only its live emulator state. A plain
 * terminal that later runs `claude` therefore scrolls identically to a
 * claude-launched session.
 */
internal fun scrollBranchFor(mouseTracking: Boolean, altBuffer: Boolean): ScrollBranch =
    when {
        mouseTracking -> ScrollBranch.WHEEL
        altBuffer -> ScrollBranch.PAGE
        else -> ScrollBranch.LOCAL
    }

/**
 * The SGR mouse-wheel report for one wheel notch: `ESC [ < button ; 1 ; 1 M`,
 * button 64 = wheel up ([scrollUp]) / 65 = wheel down. The leading 0x1B is
 * REQUIRED — omitting it (the pre-Task-7 bug) made the report print as literal
 * "[<64;1;1M" phantom text instead of scrolling. [internal] so the regression
 * test can pin the 0x1B introducer.
 */
internal fun sgrWheelBytes(scrollUp: Boolean): ByteArray {
    val button = if (scrollUp) 64 else 65
    return byteArrayOf(0x1b) + "[<$button;1;1M".toByteArray(Charsets.US_ASCII)
}

/**
 * The conventional page key: PgUp (`ESC[5~`) for [scrollUp], else PgDn
 * (`ESC[6~`). [internal] so both the swipe ([deliverScroll]) and the button
 * ([deliverButtonScroll]) share one definition — no duplicated byte literals.
 */
internal fun pageKeyBytes(scrollUp: Boolean): ByteArray =
    if (scrollUp) {
        byteArrayOf(0x1b, '['.code.toByte(), '5'.code.toByte(), '~'.code.toByte())
    } else {
        byteArrayOf(0x1b, '['.code.toByte(), '6'.code.toByte(), '~'.code.toByte())
    }

/**
 * The per-line arrow key a PAGE-branch scroll step emits: Up (`ESC[A`) for
 * [scrollUp], else Down (`ESC[B`). Upstream-Termux parity: TerminalView's own
 * alt-buffer scroll sends one ARROW per row of travel, never a full PgUp/PgDn
 * per notch (the old page-per-20px behavior turned a 100px drag into 5 whole
 * pages). [internal] so the unit test can pin the byte shape.
 */
internal fun arrowKeyBytes(scrollUp: Boolean): ByteArray =
    if (scrollUp) {
        byteArrayOf(0x1b, '['.code.toByte(), 'A'.code.toByte())
    } else {
        byteArrayOf(0x1b, '['.code.toByte(), 'B'.code.toByte())
    }

/** Wheel notches a single PgUp/PgDn BUTTON press emits in a mouse-tracking TUI. */
private const val WHEEL_NOTCHES_PER_PAGE = 3

// =========================================================================
// Swipe-scroll physics — pure, unit-tested seams
// =========================================================================
//
// Every DECISION the gesture/fling machinery makes lives here as a pure
// function or a plain class with no Android/Compose types, matching how
// [scrollBranchFor]/[sgrWheelBytes] are already pure. [ZellijScrollEngine]
// below is only the plumbing that wires these into coroutines + the wire.

/**
 * Pre-layout fallback for the px→row gain (the view reports height 0 before
 * its first layout pass; the emulator can briefly report 0 rows).
 */
internal const val FALLBACK_ROW_HEIGHT_PX = 20f

/**
 * WHEEL gain — SGR wheel notches per ROW-HEIGHT of finger travel.
 * 1.0 = one notch per row of movement, i.e. finger-follows-content for TUIs
 * that scroll one line per notch (claude does).
 *
 * TUNING KNOB — needs Fold feel-testing with claude live: raise (e.g. 1.5,
 * 2.0) if wheel scrolling feels too slow to keep up with the finger, lower
 * (e.g. 0.5) if it overshoots. Brandon device-validates; do not guess here.
 */
internal const val WHEEL_NOTCH_PER_ROW = 1.0f

/**
 * Ceiling on SGR wheel notches emitted per second (drag AND fling). Each
 * notch is a Tailscale round-trip that comes back as a near-full-frame ANSI
 * repaint — an uncapped fast swipe floods the link and stutters. ~30/s keeps
 * motion visually continuous while bounding the repaint traffic.
 */
internal const val WHEEL_MAX_NOTCHES_PER_SEC = 30f

/**
 * Max wheel notches allowed to queue. Backlog beyond this COLLAPSES (excess
 * is dropped, not queued) so a monster swipe can never bank hundreds of
 * round-trips that keep scrolling long after the finger stopped.
 */
internal const val WHEEL_MAX_BACKLOG_NOTCHES = 8

/**
 * Instantaneous burst allowance: the pacer's budget starts (and refills to)
 * this many notches, so the FIRST notch(es) of a slow deliberate scroll emit
 * immediately instead of waiting a frame for budget to accrue.
 */
internal const val WHEEL_BURST_BUDGET = 3f

/** PAGE branch safety: max per-line arrow keys emitted per tick (excess drops). */
internal const val PAGE_MAX_ARROWS_PER_TICK = 3

/** Minimum |finger-up velocity| (px/s) that starts a decay fling. */
internal const val FLING_MIN_VELOCITY_PX_PER_S = 100f

/** dt assumed for the drainer's first frame (one 60Hz frame), before deltas exist. */
private const val DEFAULT_FRAME_NANOS = 16_666_667L

/**
 * The px→row gain, derived from the LIVE view: one terminal row's height in
 * device pixels. Replaces the hardcoded 20f (real Fold row heights are
 * ~37-44px, so 20f made every swipe scroll ~2x the finger travel — the
 * "line-jump lurch"). Falls back pre-layout / on a zero-row emulator.
 */
internal fun terminalRowHeightPx(
    viewHeightPx: Int,
    rows: Int,
    fallbackPx: Float = FALLBACK_ROW_HEIGHT_PX,
): Float =
    if (viewHeightPx > 0 && rows > 0) viewHeightPx.toFloat() / rows else fallbackPx

/**
 * Pixels of finger/fling travel per emitted scroll STEP for [branch]:
 * LOCAL/PAGE move one row per row-height (1:1 finger:content); WHEEL divides
 * by [wheelNotchPerRow] so the notch rate is tunable independently.
 */
internal fun pixelsPerScrollStep(
    rowHeightPx: Float,
    branch: ScrollBranch,
    wheelNotchPerRow: Float = WHEEL_NOTCH_PER_ROW,
): Float = when (branch) {
    ScrollBranch.WHEEL -> rowHeightPx / wheelNotchPerRow
    ScrollBranch.PAGE, ScrollBranch.LOCAL -> rowHeightPx
}

/**
 * PAGE-branch coalescing: clamp the steps one tick may emit as arrow keys to
 * ±[maxPerTick]; the excess is DROPPED (collapsed), never queued.
 */
internal fun coalescedArrowCount(
    requestedSteps: Int,
    maxPerTick: Int = PAGE_MAX_ARROWS_PER_TICK,
): Int = requestedSteps.coerceIn(-maxPerTick, maxPerTick)

/** Whether a finger-up velocity is decisive enough to start a fling. */
internal fun shouldFling(
    velocityPxPerS: Float,
    minVelocityPxPerS: Float = FLING_MIN_VELOCITY_PX_PER_S,
): Boolean = kotlin.math.abs(velocityPxPerS) >= minVelocityPxPerS

/**
 * Pixel→step integerizer shared by the drag loop and the fling: accumulate
 * fractional travel, emit whole steps, KEEP the remainder (so 3 × 15px at a
 * 40px row height correctly yields one step, not zero). Truncates toward
 * zero symmetrically for both scroll directions.
 */
internal class ScrollLineAccumulator {
    private var remainderPx = 0f

    /** Add [dyPx] of travel; returns whole steps at [stepPx] px per step. */
    fun add(dyPx: Float, stepPx: Float): Int {
        if (stepPx <= 0f) return 0 // div-zero guard (pre-layout weirdness)
        remainderPx += dyPx
        val steps = (remainderPx / stepPx).toInt()
        remainderPx -= steps * stepPx
        return steps
    }

    fun reset() {
        remainderPx = 0f
    }
}

/**
 * Frame-paced, coalescing rate limiter for SGR wheel notches. Pure math —
 * callers [add] signed notches (+ = toward history) as the finger/fling
 * produces them and [drain] once per frame with the frame dt; emission is
 * capped at [maxNotchesPerSecond] via a budget that accrues with real time
 * (and starts at [burstBudget] so the first notches are instant). Backlog is
 * clamped to ±[maxBacklogNotches]: excess COLLAPSES instead of queueing.
 */
internal class WheelNotchPacer(
    private val maxNotchesPerSecond: Float = WHEEL_MAX_NOTCHES_PER_SEC,
    private val maxBacklogNotches: Int = WHEEL_MAX_BACKLOG_NOTCHES,
    private val burstBudget: Float = WHEEL_BURST_BUDGET,
) {
    private var pendingNotches = 0
    private var budget = burstBudget

    val hasPending: Boolean get() = pendingNotches != 0

    fun add(notches: Int) {
        pendingNotches = (pendingNotches + notches)
            .coerceIn(-maxBacklogNotches, maxBacklogNotches)
    }

    /**
     * One frame tick: returns the SIGNED notch count to emit now (0 when the
     * budget hasn't accrued a whole notch yet, or nothing is pending).
     */
    fun drain(frameDtNanos: Long): Int {
        budget = (budget + maxNotchesPerSecond * (frameDtNanos / 1_000_000_000f))
            .coerceAtMost(burstBudget.coerceAtLeast(1f))
        if (pendingNotches == 0) return 0
        val allowed = budget.toInt()
        if (allowed <= 0) return 0
        val emit = pendingNotches.coerceIn(-allowed, allowed)
        pendingNotches -= emit
        budget -= kotlin.math.abs(emit)
        return emit
    }

    /**
     * Drop the backlog (touch-to-stop / programmatic reset / mouse-tracking
     * lost at drain time). NOTE: budget does NOT accrue here — it only
     * accrues inside [drain], so once the drainer exits (backlog empty or
     * cleared) the budget is FROZEN at its last value. After a fling that
     * exhausted the budget, the next scroll's first notch can therefore wait
     * up to ~2 drain frames (60Hz) for budget to accrue — don't "fix" a
     * perceived first-notch lag by raising [burstBudget] without checking
     * this path first.
     */
    fun clear() {
        pendingNotches = 0
    }
}

/**
 * One wheel drain tick, decided purely: notches queued for the WHEEL may only
 * hit the wire while mouse tracking is STILL on at DRAIN time (the file's
 * "live state, never cached" rule applies to the queue too). If the TUI
 * dropped mouse tracking mid-fling (e.g. claude exiting on its own), the
 * queued notches are STALE — emitting them would print literal
 * `ESC[<6x;1;1M` text on the shell prompt — so the backlog clears and
 * nothing emits. Otherwise emit per the pacer's rate budget.
 */
internal fun drainWheelTick(
    pacer: WheelNotchPacer,
    mouseTrackingNow: Boolean,
    frameDtNanos: Long,
): Int {
    if (!mouseTrackingNow) {
        pacer.clear()
        return 0
    }
    return pacer.drain(frameDtNanos)
}

// =========================================================================
// Swipe-scroll engine — coroutine/wire plumbing over the pure seams
// =========================================================================

/**
 * Runtime glue for the swipe physics: owns the shared px→step accumulator,
 * the post-finger-up decay-fling [Job], and the frame-paced SGR-wheel
 * drainer. One instance per screen; everything runs on the composition's
 * main-thread scope (no cross-thread state).
 *
 * The providers are invoked on EVERY tick (they close over Compose
 * MutableState) — never cached — preserving the live-state rule: the scroll
 * branch is re-resolved from the emulator flags each time, so a TUI flipping
 * mouse tracking mid-fling is honored on the very next frame.
 *
 * AIRTIGHTNESS (commit 23aaadf): this engine never touches pointer events.
 * The gesture loop's consumption/ownership rules are unchanged; a fling only
 * feeds synthetic pixel deltas into [feedPixels], so no touch can leak to the
 * child TerminalView through anything added here.
 */
internal class ZellijScrollEngine(
    private val scope: CoroutineScope,
    private val viewProvider: () -> TerminalView?,
    private val clientProvider: () -> ZellijWebSocketClient?,
) {
    private val accumulator = ScrollLineAccumulator()
    private val pacer = WheelNotchPacer()
    private var flingJob: Job? = null
    private var drainJob: Job? = null

    /** Drag promotion resets the px remainder (legacy `accumulator = 0f`). */
    fun resetAccumulator() = accumulator.reset()

    /**
     * Feed vertical travel in pixels — finger dy during a drag, or one decay
     * frame's delta during a fling (>0 = toward history, matching the legacy
     * wholeLines sign). Resolves the LIVE branch + LIVE row-height gain,
     * integerizes via the shared accumulator, then delivers:
     *   WHEEL → queue on the pacer (frame-paced, rate-capped, coalescing);
     *   PAGE  → per-line arrows, hard-capped per tick (excess collapses);
     *   LOCAL → move the emulator's own transcript window.
     */
    fun feedPixels(dyPx: Float) {
        val v = viewProvider() ?: return
        val emu = v.mEmulator ?: return
        val branch = scrollBranchFor(emu.isMouseTrackingActive, emu.isAlternateBufferActive)
        val stepPx = pixelsPerScrollStep(terminalRowHeightPx(v.height, emu.mRows), branch)
        val steps = accumulator.add(dyPx, stepPx)
        if (steps == 0) return
        when (branch) {
            ScrollBranch.WHEEL -> {
                pacer.add(steps)
                ensureDrainerRunning()
            }
            ScrollBranch.PAGE -> {
                val n = coalescedArrowCount(steps)
                if (n != 0) {
                    val client = clientProvider() ?: return
                    val seq = arrowKeyBytes(scrollUp = n > 0)
                    repeat(kotlin.math.abs(n)) { client.sendBytes(seq) }
                }
            }
            ScrollBranch.LOCAL -> {
                val delta = -steps
                val maxBack = -emu.screen.activeTranscriptRows
                val newTop = (v.topRow + delta).coerceIn(maxBack, 0)
                if (newTop == v.topRow) {
                    // Transcript edge: a running fling has nothing left to
                    // move — stop instead of decaying silently for seconds.
                    cancelFling()
                } else {
                    v.topRow = newTop
                    v.onScreenUpdated()
                }
            }
        }
    }

    /**
     * Start the post-finger-up fling: standard Android decay physics feeding
     * per-frame pixel DELTAS through the SAME [feedPixels] path the finger
     * used (branch + gain re-resolved live on every frame).
     */
    fun startFling(velocityPxPerS: Float, decaySpec: DecayAnimationSpec<Float>) {
        cancelFling()
        flingJob = scope.launch {
            var lastValue = 0f
            AnimationState(initialValue = 0f, initialVelocity = velocityPxPerS)
                .animateDecay(decaySpec) {
                    val frameDelta = value - lastValue
                    lastValue = value
                    feedPixels(frameDelta)
                }
        }
    }

    fun cancelFling() {
        flingJob?.cancel()
        flingJob = null
    }

    /**
     * Hard stop: kill the fling, drop the queued wheel backlog, zero the px
     * remainder. Called on every touch-down (touch-to-stop) and by every
     * programmatic view reset (`setTopRow(0)` sites: typing, extra keys,
     * paste) — a fling must never keep scrolling under fresh input.
     */
    fun stopAll() {
        cancelFling()
        pacer.clear()
        accumulator.reset()
    }

    /**
     * Frame-paced wheel emission: a single lazily-(re)started coroutine
     * drains the pacer on frame boundaries until the backlog empties, then
     * exits (no perpetual frame loop — same battery rule as EmberParticles).
     * Both the drag path and the fling path enqueue through [feedPixels] into
     * this ONE pacer, so the [WHEEL_MAX_NOTCHES_PER_SEC] cap holds for fast
     * finger swipes too, not just flings.
     */
    private fun ensureDrainerRunning() {
        if (drainJob?.isActive == true) return
        drainJob = scope.launch {
            var lastFrameNanos = 0L
            while (pacer.hasPending) {
                withFrameNanos { frameNanos ->
                    val dt =
                        if (lastFrameNanos == 0L) DEFAULT_FRAME_NANOS
                        else frameNanos - lastFrameNanos
                    lastFrameNanos = frameNanos
                    // LIVE branch re-check at DRAIN time, not just enqueue
                    // time: mouse tracking can drop mid-fling (the TUI
                    // exiting on its own) — [drainWheelTick] then clears the
                    // stale backlog instead of printing literal wheel
                    // reports on the shell prompt.
                    val mouseTrackingNow =
                        viewProvider()?.mEmulator?.isMouseTrackingActive == true
                    val emit = drainWheelTick(pacer, mouseTrackingNow, dt)
                    if (emit != 0) {
                        clientProvider()?.let { client ->
                            val seq = sgrWheelBytes(scrollUp = emit > 0)
                            repeat(kotlin.math.abs(emit)) { client.sendBytes(seq) }
                        }
                    }
                }
            }
        }
    }
}

/**
 * Deliver ONE PgUp/PgDn BUTTON press through the SAME live-state branch as the
 * swipe path ([scrollBranchFor], read at press time), so the button matches
 * whatever the running TUI actually honors. [delta] < 0 = toward history
 * (PgUp), > 0 = toward live (PgDn); its magnitude is the local-scroll line
 * count. Critically, in a mouse-tracking TUI (claude) the button now sends the
 * WHEEL — bare PgUp is ignored there, which is why the button "did nothing"
 * even after a swipe worked. PAGE = one conventional page key (a no-mouse
 * pager); LOCAL = scroll the emulator's own transcript by [delta] lines.
 */
private fun deliverButtonScroll(
    v: TerminalView?,
    client: ZellijWebSocketClient,
    delta: Int,
) {
    val emu = v?.mEmulator ?: return
    val scrollUp = delta < 0
    when (scrollBranchFor(emu.isMouseTrackingActive, emu.isAlternateBufferActive)) {
        ScrollBranch.WHEEL -> {
            val seq = sgrWheelBytes(scrollUp)
            repeat(WHEEL_NOTCHES_PER_PAGE) { client.sendBytes(seq) }
        }
        ScrollBranch.PAGE -> client.sendBytes(pageKeyBytes(scrollUp))
        ScrollBranch.LOCAL -> {
            val maxBack = -emu.screen.activeTranscriptRows
            val newTop = (v.topRow + delta).coerceIn(maxBack, 0)
            v.topRow = newTop
            v.onScreenUpdated()
        }
    }
}

/**
 * Focus the terminal and pop the soft keyboard — the entire effect of a tap
 * while mouse tracking is active (zero bytes to the emulator). Mirrors the
 * TerminalView's own onSingleTapUp, which handles the not-tracking case.
 */
private fun focusAndShowKeyboard(v: TerminalView?) {
    v ?: return
    v.requestFocus()
    (v.context.getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager)
        ?.showSoftInput(v, InputMethodManager.SHOW_IMPLICIT)
}

/**
 * Wrap [text] in bracketed-paste sequences: ESC[200~ before, ESC[201~ after.
 *
 * Zellij's two-socket protocol carries paste as raw PTY bytes (no separate
 * control frame like CliAgentWebSocket.sendPaste). We emit the bracketed
 * sequences inline so the receiving app (claude/gemini/bash) can distinguish
 * pasted text from typed input. Internal so the test below can pin the wire
 * shape.
 */
internal fun buildBracketedPaste(text: String): ByteArray {
    val prefix = byteArrayOf(0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '0'.code.toByte(), '~'.code.toByte())
    val suffix = byteArrayOf(0x1b, '['.code.toByte(), '2'.code.toByte(), '0'.code.toByte(), '1'.code.toByte(), '~'.code.toByte())
    val body = text.toByteArray(Charsets.UTF_8)
    val out = ByteArray(prefix.size + body.size + suffix.size)
    System.arraycopy(prefix, 0, out, 0, prefix.size)
    System.arraycopy(body, 0, out, prefix.size, body.size)
    System.arraycopy(suffix, 0, out, prefix.size + body.size, suffix.size)
    return out
}

// =========================================================================
// Local visual building blocks (mirrors TerminalScreen.kt internals)
// =========================================================================

private enum class ZellijBannerKind { Info, Warn, Error }

@Composable
private fun ReconnectBanner(
    text: String,
    kind: ZellijBannerKind,
    modifier: Modifier = Modifier,
) {
    val bg: Color = when (kind) {
        ZellijBannerKind.Info -> Neutral500.copy(alpha = 0.25f)
        ZellijBannerKind.Warn -> BbxAccent.copy(alpha = 0.18f)
        ZellijBannerKind.Error -> BbxAccent.copy(alpha = 0.28f)
    }
    val fg: Color = BbxWhite
    val glyph: String = when (kind) {
        ZellijBannerKind.Info -> "•"
        ZellijBannerKind.Warn -> "⚠"
        ZellijBannerKind.Error -> "⚠"
    }
    Row(
        modifier = modifier
            .fillMaxWidth()
            .background(bg)
            .padding(horizontal = 12.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(
            text = glyph,
            color = fg,
            fontWeight = FontWeight.Bold,
            fontSize = 14.sp,
        )
        Text(
            text = "  $text",
            color = fg,
            fontSize = 13.sp,
            fontFamily = FontFamily.Monospace,
        )
    }
}
