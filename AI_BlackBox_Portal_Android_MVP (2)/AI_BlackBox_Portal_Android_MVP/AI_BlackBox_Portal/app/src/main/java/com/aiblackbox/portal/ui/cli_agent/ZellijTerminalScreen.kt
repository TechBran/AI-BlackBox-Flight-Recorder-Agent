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
// What's reused: ExtraKeysBar, WhisperMicButton, the Termux TerminalView +
// TerminalSession bridging trick (local sleep child, append bytes to the
// emulator directly), the ZellijBannerKind/ReconnectBanner visual contract.
//
// What differs from TerminalScreen:
//   • Client: ZellijWebSocketClient(origin, sessionName, sessionToken, scope)
//     instead of CliAgentWebSocket(baseUrl, sessionId, params, callbacks).
//   • Connection: lifecycle ownership via DisposableEffect that calls
//     client.close() on dispose — token is transient (audit I7) and dies
//     with this composition.
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
import androidx.compose.foundation.background
import androidx.compose.foundation.gestures.detectVerticalDragGestures
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.input.pointer.pointerInput
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

private const val TAG = "ZellijTerminalScreen"

private const val DEFAULT_COLS = 80
private const val DEFAULT_ROWS = 24
private const val TRANSCRIPT_ROWS = 2000

/**
 * Zellij-backed terminal Composable. Hosts a Termux [TerminalView] inside
 * [AndroidView], proxies bytes between the emulator and a freshly-minted
 * [ZellijWebSocketClient], and shows an [ExtraKeysBar] + [WhisperMicButton]
 * at the bottom.
 *
 * Lifecycle: the [ZellijWebSocketClient] is constructed when this composable
 * enters composition and closed in [DisposableEffect.onDispose]. The token
 * carried in [session] is transient (audit I7) and isn't persisted anywhere
 * outside this composition — after the WS handshake succeeds the server
 * holds session state.
 *
 * Back behavior: detach only — the zellij session survives in the
 * orchestrator. Killing happens through [SessionSwitcherTopBar]'s long-press
 * confirm flow.
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
    val coroutineScope = rememberCoroutineScope()

    // --- Connection state surfaced to the banner ---------------------------
    var bannerText by remember { mutableStateOf<String?>("Connecting…") }
    var bannerKind by remember { mutableStateOf(ZellijBannerKind.Info) }

    // --- Termux view / session references -----------------------------------
    var terminalView by remember { mutableStateOf<TerminalView?>(null) }
    var terminalSession by remember { mutableStateOf<TerminalSession?>(null) }

    // --- Grid dimensions; pushed via sendResize whenever they change --------
    var cols by remember { mutableStateOf(DEFAULT_COLS) }
    var rows by remember { mutableStateOf(DEFAULT_ROWS) }

    // --- ZellijWebSocketClient construction --------------------------------
    //
    // Origin defaults to BlackBoxApi.getBaseUrl(); ZellijWebSocketClient
    // normalises http(s)/ws(s) variants internally. The token + sessionName
    // come from the launch response (passed in via [session]). webClientId
    // is auto-generated (UUID) inside the client.
    //
    // remember(session.name) so that switching between sessions while
    // staying in this Terminal branch swaps out the client cleanly rather
    // than trying to retarget an open socket — simpler and matches the
    // legacy TerminalScreen's `remember(operator, provider, appSlug)` key.
    val client: ZellijWebSocketClient = remember(session.name) {
        ZellijWebSocketClient(
            origin = api.getBaseUrl(),
            sessionName = session.name,
            sessionToken = session.token,
            coroutineScope = coroutineScope,
        )
    }

    // --- Connect on enter; ensure close on dispose --------------------------
    //
    // DisposableEffect keys on [client] so a session swap (new ZellijSession
    // in props → new client via remember(session.name)) tears down the old
    // socket and re-runs the connect block for the new one.
    DisposableEffect(client) {
        client.connect(object : ZellijWebSocketClient.Listener {
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
        })

        onDispose {
            try {
                client.close()
            } catch (_: Throwable) {
            }
        }
    }

    // --- System back: detach only -------------------------------------------
    BackHandler(enabled = true) {
        onBack()
    }

    // --- Push resize whenever cols/rows change ------------------------------
    LaunchedEffect(cols, rows) {
        Log.d(TAG, "Resize → ${cols}x${rows}")
        try {
            terminalSession?.updateSize(cols, rows)
        } catch (t: Throwable) {
            Log.w(TAG, "session.updateSize failed", t)
        }
        client.sendResize(cols = cols, rows = rows)
    }

    // --- Compose UI ---------------------------------------------------------
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(BbxBlack)
            .statusBarsPadding()
            .navigationBarsPadding()
            .imePadding(),
    ) {
        // --- Reconnect / status banner ---
        val bannerLine = bannerText
        if (bannerLine != null) {
            ReconnectBanner(text = bannerLine, kind = bannerKind)
        }

        // --- Terminal surface --------------------------------------------------
        //
        // The outer Box wraps the AndroidView<TerminalView> with a Compose
        // pointerInput layer that intercepts vertical drags and converts them
        // to terminal scroll operations BEFORE they reach the TerminalView.
        //
        // T23 device QA (2026-05-26): without this interception, claude turns
        // on mouse-tracking mode (CSI ?1000h/?1003h/?1006h) and the underlying
        // Termux TerminalView forwards every touch as an SGR mouse escape
        // sequence ("<65;44;17M") that visibly accumulates in the prompt. The
        // ExtraKeysBar's PgUp/PgDn buttons work but you can't actually swipe
        // — which is the natural gesture on a phone.
        //
        // Implementation:
        //   - detectVerticalDragGestures only fires after Compose's touchSlop
        //     is exceeded vertically, so single taps still pass through to
        //     the TerminalView for IME focus.
        //   - dragAmount > 0 (finger moving down) → reveal history above
        //     (topRow more negative) — matches platform-natural scroll.
        //   - In alt-screen buffer (vim, nano, claude's full-screen TUI),
        //     dispatch PgUp/PgDn over the WebSocket so the app handles it.
        //   - change.consume() prevents the gesture from bubbling to the
        //     TerminalView, so no mouse-tracking sequences get emitted.
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = true)
                .background(BbxBlack)
                .pointerInput(Unit) {
                    // Fractional pixels we haven't yet converted to a whole
                    // scroll-line. Reset on drag start/end so a slow swipe
                    // doesn't accumulate stale fractional movement.
                    var accumulator = 0f
                    // ~20 device pixels per scroll line. Picked to feel close
                    // to the desktop wheel speed; tuneable in v1.1.
                    val pixelsPerLine = 20f
                    detectVerticalDragGestures(
                        onDragStart = { accumulator = 0f },
                        onDragEnd = { accumulator = 0f },
                        onDragCancel = { accumulator = 0f },
                    ) { change, dragAmount ->
                        accumulator += dragAmount
                        val wholeLines = (accumulator / pixelsPerLine).toInt()
                        if (wholeLines != 0) {
                            val v = terminalView
                            val emu = v?.mEmulator
                            if (v != null && emu != null) {
                                // Natural scroll: dragAmount > 0 (down) →
                                // user wants HISTORY (scroll up in scrollback).
                                // wholeLines > 0 → scroll up. We branch the
                                // delivery on what the TUI expects.
                                val scrollUp = wholeLines > 0
                                when {
                                    // Case 1: TUI has mouse tracking enabled
                                    // (claude, htop, mc, any modern full-screen
                                    // TUI). Browsers translate wheel events to
                                    // SGR mouse buttons 64 (wheel up) / 65
                                    // (wheel down). The TUI binds those to its
                                    // own scroll-history command. T23 fix
                                    // 2026-05-26: previously we sent PgUp/PgDn
                                    // here, which claude ignores in alt-buffer
                                    // mode — "swipes did nothing."
                                    emu.isMouseTrackingActive -> {
                                        val button = if (scrollUp) 64 else 65
                                        // ESC[<{button};{col};{row}M
                                        // col/row are 1-indexed; we use the
                                        // top-left because wheel events don't
                                        // care about position for most TUIs.
                                        val seq = "[<$button;1;1M".toByteArray(
                                            Charsets.US_ASCII,
                                        )
                                        repeat(kotlin.math.abs(wholeLines)) {
                                            client.sendBytes(seq)
                                        }
                                    }

                                    // Case 2: alt-buffer TUI WITHOUT mouse
                                    // tracking (rare — most modern TUIs turn
                                    // on mouse tracking; this is the fallback
                                    // for less / more / man pages etc.).
                                    // PgUp/PgDn is the conventional binding.
                                    emu.isAlternateBufferActive -> {
                                        val seq: ByteArray = if (scrollUp) {
                                            // PgUp = ESC[5~
                                            byteArrayOf(0x1b, '['.code.toByte(), '5'.code.toByte(), '~'.code.toByte())
                                        } else {
                                            // PgDn = ESC[6~
                                            byteArrayOf(0x1b, '['.code.toByte(), '6'.code.toByte(), '~'.code.toByte())
                                        }
                                        repeat(kotlin.math.abs(wholeLines)) {
                                            client.sendBytes(seq)
                                        }
                                    }

                                    // Case 3: Normal-buffer shell (bash prompt
                                    // after a command). The emulator owns the
                                    // scrollback — manipulate topRow directly,
                                    // no bytes go to the WebSocket.
                                    else -> {
                                        val delta = -wholeLines
                                        val maxBack = -emu.screen.activeTranscriptRows
                                        val newTop = (v.topRow + delta).coerceIn(maxBack, 0)
                                        v.topRow = newTop
                                        v.onScreenUpdated()
                                    }
                                }
                            }
                            accumulator -= wholeLines * pixelsPerLine
                        }
                        change.consume()
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
                                view.setTopRow(0)
                                client.sendBytes(bytes)
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
                            view.setTopRow(0)
                            client.sendBytes(bytes)
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
                    val sess = TerminalSession(
                        /* shellPath      = */ "/system/bin/sleep",
                        /* cwd            = */ "/",
                        /* args           = */ arrayOf("sleep", "999999"),
                        /* env            = */ arrayOf<String>(),
                        /* transcriptRows = */ TRANSCRIPT_ROWS,
                        /* client         = */ sessionClient,
                    )

                    view.attachSession(sess)

                    terminalView = view
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
                    }
                },
            )
        }

        // --- Extra-keys bar + mic ----------------------------------------------
        ExtraKeysBar(
            onKeyBytes = { bytes ->
                terminalView?.setTopRow(0)
                client.sendBytes(bytes)
            },
            onScrollLines = { delta ->
                val v = terminalView ?: return@ExtraKeysBar
                val emu = v.mEmulator ?: return@ExtraKeysBar
                if (emu.isAlternateBufferActive) {
                    val seq: ByteArray = if (delta < 0) {
                        // PgUp = ESC[5~
                        byteArrayOf(0x1b, '['.code.toByte(), '5'.code.toByte(), '~'.code.toByte())
                    } else {
                        // PgDn = ESC[6~
                        byteArrayOf(0x1b, '['.code.toByte(), '6'.code.toByte(), '~'.code.toByte())
                    }
                    client.sendBytes(seq)
                } else {
                    val maxBack = -emu.screen.activeTranscriptRows
                    val newTop = (v.topRow + delta).coerceIn(maxBack, 0)
                    v.topRow = newTop
                    v.onScreenUpdated()
                }
            },
            micSlot = {
                WhisperMicButton(
                    onTranscript = { transcript ->
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
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

// =========================================================================
// Helpers
// =========================================================================

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
private fun ReconnectBanner(text: String, kind: ZellijBannerKind) {
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
        modifier = Modifier
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
