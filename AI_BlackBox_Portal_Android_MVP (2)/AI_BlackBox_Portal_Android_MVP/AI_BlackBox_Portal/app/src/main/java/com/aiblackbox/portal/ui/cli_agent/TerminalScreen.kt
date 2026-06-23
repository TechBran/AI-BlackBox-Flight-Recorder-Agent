package com.aiblackbox.portal.ui.cli_agent

// TerminalScreen — wraps Termux's TerminalView via AndroidView and wires
// the WebSocket bytes / extra-keys bar / Whisper mic.
//
// Bridging strategy (Termux 0.118 — TerminalSession is FINAL):
//   1. Construct a real TerminalSession with a harmless local child
//      ("/system/bin/sleep 999999") — TerminalView.attachSession requires
//      a TerminalSession instance.
//   2. Feed incoming WebSocket bytes directly to the emulator via
//      TerminalEmulator.append(byte[], int) — bypassing the local PTY.
//   3. Intercept keystrokes in TerminalViewClient.onCodePoint and forward
//      them to the WebSocket; return true to prevent default routing
//      through session.write() (which would go to the dead local sleep
//      process).
//   4. Resize: call session.updateSize(cols, rows) to keep the emulator
//      grid right; call ws.sendResize(cols, rows) to keep the orchestrator
//      tmux pty right.

import android.content.Context
import android.util.Log
import android.view.KeyEvent
import android.view.MotionEvent
import android.view.inputmethod.InputMethodManager
import androidx.activity.compose.BackHandler
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Box
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.navigationBarsPadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.statusBarsPadding
import androidx.compose.ui.layout.onSizeChanged
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.runtime.Composable
import androidx.compose.runtime.DisposableEffect
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalDensity
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.unit.dp
import androidx.compose.ui.unit.sp
import androidx.compose.ui.viewinterop.AndroidView
import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.ui.feedback.rememberPressFeedback
import com.aiblackbox.portal.ui.theme.BbxAccent
import com.aiblackbox.portal.ui.theme.BbxBlack
import com.aiblackbox.portal.ui.theme.BbxWhite
import com.aiblackbox.portal.ui.theme.Neutral500

import com.termux.terminal.TerminalEmulator
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import com.termux.view.TerminalView
import com.termux.view.TerminalViewClient

private const val TAG = "TerminalScreen"

private const val DEFAULT_COLS = 80
private const val DEFAULT_ROWS = 24
private const val TRANSCRIPT_ROWS = 2000

/**
 * The CLI Agent terminal Composable. Hosts a Termux [TerminalView] inside
 * [AndroidView], proxies bytes between the emulator and a
 * [CliAgentWebSocket], and shows an [ExtraKeysBar] + [WhisperMicButton] at
 * the bottom.
 *
 * Back behavior: detach only — the tmux session survives in the
 * Orchestrator. Use [AppFolderPicker]'s long-press → Kill flow to
 * actually terminate.
 */
@Composable
fun TerminalScreen(
    api: BlackBoxApi,
    operator: String,
    appSlug: String,        // "" = Apps/ root
    appName: String,        // displayed in TopAppBar
    provider: String,
    onBack: () -> Unit,
    modifier: Modifier = Modifier,
) {
    val density = LocalDensity.current

    // --- Connection state surfaced to the banner ----------------------
    var bannerText by remember { mutableStateOf<String?>("Connecting…") }
    var bannerKind by remember { mutableStateOf(BannerKind.Info) }

    // --- Build session id + WebSocket once ----------------------------
    val sessionId: String = remember(operator, provider, appSlug) {
        cliAgentSessionId(operator = operator, provider = provider, appSlug = appSlug)
    }

    // Reference to the Termux TerminalView so callbacks can poke it.
    var terminalView by remember { mutableStateOf<TerminalView?>(null) }
    // Reference to the active TerminalSession so onBytes can write into it.
    var terminalSession by remember { mutableStateOf<TerminalSession?>(null) }

    // We capture cols/rows from Compose layout & ship them as resize
    // frames whenever they change. Initial values match server defaults.
    var cols by remember { mutableStateOf(DEFAULT_COLS) }
    var rows by remember { mutableStateOf(DEFAULT_ROWS) }

    // Build the WebSocket. baseUrl comes from BlackBoxApi.getBaseUrl().
    val ws: CliAgentWebSocket = remember(operator, provider, appSlug) {
        val baseUrl = api.getBaseUrl()
        val params = mapOf(
            "op" to operator,
            "provider" to provider,
            "app" to appSlug,
            "cols" to cols.toString(),
            "rows" to rows.toString(),
        )
        CliAgentWebSocket(
            baseUrl = baseUrl,
            sessionId = sessionId,
            params = params,
            callbacks = object : CliAgentWebSocket.Callbacks {
                override fun onOpen(state: String) {
                    Log.d(TAG, "ws onOpen state=$state")
                    bannerText = null
                }

                override fun onBytes(bytes: ByteArray) {
                    // Feed PTY bytes directly into the emulator on the UI
                    // thread. We bypass session.write() (which would route
                    // to the dead local sleep PTY) and call
                    // TerminalEmulator.append(byte[], int) directly.
                    val view = terminalView ?: return
                    view.post {
                        try {
                            val session = terminalSession ?: return@post
                            val emulator: TerminalEmulator? = session.emulator
                            if (emulator != null) {
                                emulator.append(bytes, bytes.size)
                                view.onScreenUpdated()
                            } else {
                                Log.w(TAG, "No emulator on session — dropping ${bytes.size} bytes")
                            }
                        } catch (t: Throwable) {
                            Log.e(TAG, "Failed to feed bytes to emulator", t)
                        }
                    }
                }

                override fun onError(code: String, message: String) {
                    Log.w(TAG, "ws onError code=$code message=$message")
                    bannerText = "Error: $message"
                    bannerKind = BannerKind.Error
                }

                override fun onClosed(code: Int, reason: String) {
                    Log.d(TAG, "ws onClosed code=$code reason=$reason")
                    bannerText = "Disconnected (${reason.ifBlank { "code $code" }})"
                    bannerKind = BannerKind.Warn
                }

                override fun onReconnecting(attemptDelayMs: Long) {
                    val secs = "%.1f".format(attemptDelayMs / 1000.0)
                    bannerText = "Reconnecting in ${secs}s…"
                    bannerKind = BannerKind.Warn
                }
            },
        )
    }

    // --- Lifecycle: connect on first composition, close on dispose ---
    LaunchedEffect(ws) {
        ws.connect()
    }

    DisposableEffect(ws) {
        onDispose {
            try {
                ws.close()
            } catch (_: Throwable) {
            }
        }
    }

    // --- System back: detach only ---
    BackHandler(enabled = true) {
        ws.close()
        onBack()
    }

    // --- Push resize whenever cols/rows change ---
    LaunchedEffect(cols, rows) {
        Log.d(TAG, "Resize → ${cols}x${rows}")
        // Keep the emulator's grid in sync with the orchestrator pty.
        try {
            terminalSession?.updateSize(cols, rows)
        } catch (t: Throwable) {
            Log.w(TAG, "session.updateSize failed", t)
        }
        ws.sendResize(cols = cols, rows = rows)
    }

    // --- Compose UI ---------------------------------------------------
    // Insets:
    //   • statusBarsPadding   — top, behind clock/notch
    //   • navigationBarsPadding — bottom, behind gesture handle (so the
    //     ExtraKeysBar is reachable)
    //   • imePadding           — pushes the whole column above the soft
    //     keyboard when it's open (so the bar floats above the IME, not
    //     under it). Works because NativeMainActivity calls
    //     enableEdgeToEdge() and Compose reads the IME inset directly.
    Column(
        modifier = modifier
            .fillMaxSize()
            .background(BbxBlack)
            .statusBarsPadding()
            .navigationBarsPadding()
            .imePadding(),
    ) {
        // --- Top bar ---
        TerminalTopBar(
            title = "${provider.replaceFirstChar { it.uppercase() }} · ${appName.ifBlank { "Apps root" }}",
            onBack = {
                ws.close()
                onBack()
            },
            onShowKeyboard = {
                val view = terminalView ?: return@TerminalTopBar
                view.requestFocus()
                val imm = view.context
                    .getSystemService(Context.INPUT_METHOD_SERVICE) as? InputMethodManager
                imm?.showSoftInput(view, InputMethodManager.SHOW_IMPLICIT)
            },
        )

        // --- Reconnect / status banner ---
        val bannerLine = bannerText
        if (bannerLine != null) {
            ReconnectBanner(text = bannerLine, kind = bannerKind)
        }

        // --- Terminal surface (AndroidView host for Termux TerminalView) ---
        // onSizeChanged fires after every Compose layout pass — initial
        // mount, rotation, soft-keyboard show/hide, etc. Termux's
        // TerminalView reacts to its own onSizeChanged by calling
        // session.updateSize, which writes the new column/row count into
        // mEmulator.mColumns / mRows. We read it from there (one frame
        // later via post()) and propagate to our Compose state, which
        // drives the resize control frame to the server. Without this,
        // the server stays at the URL-default 80×24 forever, and any
        // ANSI cursor positioning bytes from Claude Code render at
        // coordinates that don't match the phone screen — causing the
        // "jumps multiple rows on one arrow press" visual chaos.
        Box(
            modifier = Modifier
                .fillMaxWidth()
                .weight(1f, fill = true)
                .background(BbxBlack)
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
                    // Required to receive IME (soft keyboard) input.
                    // TerminalView already implements onCreateInputConnection;
                    // we just need the view itself to be focusable in touch
                    // mode so requestFocus() actually takes focus and the IME
                    // will route keystrokes here.
                    view.isFocusable = true
                    view.isFocusableInTouchMode = true

                    // --- Session client (lifecycle / title / clipboard) ---
                    val sessionClient = object : TerminalSessionClient {
                        override fun onTextChanged(changedSession: TerminalSession) {
                            view.onScreenUpdated()
                        }

                        override fun onTitleChanged(changedSession: TerminalSession) {
                            // No-op: we use appName from the screen, not PTY.
                        }

                        override fun onSessionFinished(finishedSession: TerminalSession) {
                            // No real PTY here — local sleep finishing is
                            // not a problem; banner is driven by WS state.
                        }

                        override fun onCopyTextToClipboard(
                            session: TerminalSession,
                            text: String,
                        ) {
                            // TODO: wire to clipboard manager if/when needed.
                        }

                        override fun onPasteTextFromClipboard(session: TerminalSession) {
                            // TODO: pull from clipboard, ws.sendPaste(text).
                        }

                        override fun onBell(session: TerminalSession) {
                            // No bell sound; ignore.
                        }

                        override fun onColorsChanged(session: TerminalSession) {
                            view.onScreenUpdated()
                        }

                        override fun onTerminalCursorStateChange(state: Boolean) {
                            // No-op for cursor blink toggles.
                        }

                        override fun getTerminalCursorStyle(): Int {
                            return TerminalEmulator.DEFAULT_TERMINAL_CURSOR_STYLE
                        }

                        override fun logError(tag: String, message: String) {
                            Log.e(tag, message)
                        }

                        override fun logWarn(tag: String, message: String) {
                            Log.w(tag, message)
                        }

                        override fun logInfo(tag: String, message: String) {
                            Log.i(tag, message)
                        }

                        override fun logDebug(tag: String, message: String) {
                            Log.d(tag, message)
                        }

                        override fun logVerbose(tag: String, message: String) {
                            Log.v(tag, message)
                        }

                        override fun logStackTraceWithMessage(
                            tag: String,
                            message: String,
                            e: Exception,
                        ) {
                            Log.e(tag, message, e)
                        }

                        override fun logStackTrace(tag: String, e: Exception) {
                            Log.e(tag, "stack", e)
                        }
                    }

                    // --- TerminalView client (keystroke routing) ---
                    val viewClient = object : TerminalViewClient {
                        override fun onScale(scale: Float): Float = scale

                        override fun shouldBackButtonBeMappedToEscape(): Boolean = false
                        override fun shouldEnforceCharBasedInput(): Boolean = false
                        override fun shouldUseCtrlSpaceWorkaround(): Boolean = false
                        override fun isTerminalViewSelected(): Boolean = true

                        override fun copyModeChanged(copyMode: Boolean) {
                            // No-op
                        }

                        override fun onSingleTapUp(e: MotionEvent) {
                            // Take focus AND explicitly summon the soft
                            // keyboard. requestFocus() alone won't show the
                            // IME on Android — you have to ask for it.
                            view.requestFocus()
                            val imm = view.context
                                .getSystemService(Context.INPUT_METHOD_SERVICE)
                                as? InputMethodManager
                            imm?.showSoftInput(view, InputMethodManager.SHOW_IMPLICIT)
                        }

                        override fun onKeyDown(
                            keyCode: Int,
                            e: KeyEvent,
                            session: TerminalSession,
                        ): Boolean {
                            // Printable text arrives via onCodePoint and is
                            // already forwarded there. But special keys —
                            // Enter, Backspace, Tab, Arrows, Esc — come
                            // through onKeyDown only. Termux's default
                            // routing would send their bytes to session.write,
                            // which goes into our dead local sleep PTY and is
                            // lost. So we map them ourselves and ship bytes
                            // over the WebSocket.
                            val bytes: ByteArray? = when (keyCode) {
                                KeyEvent.KEYCODE_ENTER ->
                                    byteArrayOf(0x0d)
                                KeyEvent.KEYCODE_DEL ->        // Backspace
                                    byteArrayOf(0x7f)
                                KeyEvent.KEYCODE_FORWARD_DEL -> // Delete →
                                    byteArrayOf(0x1b, '['.code.toByte(),
                                                 '3'.code.toByte(),
                                                 '~'.code.toByte())
                                KeyEvent.KEYCODE_TAB ->
                                    byteArrayOf(0x09)
                                KeyEvent.KEYCODE_ESCAPE ->
                                    byteArrayOf(0x1b)
                                KeyEvent.KEYCODE_DPAD_UP ->
                                    byteArrayOf(0x1b, '['.code.toByte(),
                                                 'A'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_DOWN ->
                                    byteArrayOf(0x1b, '['.code.toByte(),
                                                 'B'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_LEFT ->
                                    byteArrayOf(0x1b, '['.code.toByte(),
                                                 'D'.code.toByte())
                                KeyEvent.KEYCODE_DPAD_RIGHT ->
                                    byteArrayOf(0x1b, '['.code.toByte(),
                                                 'C'.code.toByte())
                                else -> null
                            }
                            if (bytes != null) {
                                view.setTopRow(0)  // snap to live on input
                                ws.sendBytes(bytes)
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
                            // Convert codePoint to UTF-8 bytes (with Ctrl
                            // modifier if applicable) and forward to ws.
                            // Returning true tells TerminalView we handled
                            // this keystroke — don't route to local PTY.
                            val bytes: ByteArray = if (
                                ctrlDown && codePoint in 0x40..0x7F
                            ) {
                                byteArrayOf((codePoint and 0x1f).toByte())
                            } else if (ctrlDown && codePoint in 0x60..0x7A) {
                                // ctrl+lowercase a-z → 0x01..0x1A
                                byteArrayOf((codePoint and 0x1f).toByte())
                            } else {
                                String(Character.toChars(codePoint))
                                    .toByteArray(Charsets.UTF_8)
                            }
                            view.setTopRow(0)  // snap to live on input
                            ws.sendBytes(bytes)
                            return true
                        }

                        override fun onLongPress(event: MotionEvent): Boolean = false

                        override fun onEmulatorSet() {
                            Log.d(TAG, "TerminalView: emulator set")
                        }

                        override fun logError(tag: String, message: String) {
                            Log.e(tag, message)
                        }

                        override fun logWarn(tag: String, message: String) {
                            Log.w(tag, message)
                        }

                        override fun logInfo(tag: String, message: String) {
                            Log.i(tag, message)
                        }

                        override fun logDebug(tag: String, message: String) {
                            Log.d(tag, message)
                        }

                        override fun logVerbose(tag: String, message: String) {
                            Log.v(tag, message)
                        }

                        override fun logStackTraceWithMessage(
                            tag: String,
                            message: String,
                            e: Exception,
                        ) {
                            Log.e(tag, message, e)
                        }

                        override fun logStackTrace(tag: String, e: Exception) {
                            Log.e(tag, "stack", e)
                        }
                    }

                    view.setTerminalViewClient(viewClient)

                    // --- Real TerminalSession (final class — cannot
                    //     subclass). We construct it with a harmless local
                    //     child ("/system/bin/sleep 999999") that exists
                    //     but never produces output and ignores stdin.
                    //     Bridging happens by directly appending WS bytes
                    //     to the emulator and intercepting keystrokes in
                    //     onCodePoint, NOT through this PTY.
                    //
                    // argv fix (T23 device QA, 2026-05-26): `args` is the
                    // FULL argv (argv[0] included), not extra args after
                    // the binary. /system/bin/sleep is a toybox symlink
                    // that routes by argv[0]; with args=arrayOf("999999")
                    // argv[0]="999999" and toybox errors "unknown command
                    // 999999, code 127". This bug was always present but
                    // tmux's first bytes overwrote the error within ~50ms
                    // so users never saw it; ZellijTerminalScreen's
                    // slower-connect WebSocket surfaced it on Z Fold 6.
                    // Pass argv[0]="sleep" so toybox dispatches; "999999"
                    // becomes argv[1] (the duration).
                    val session = TerminalSession(
                        /* shellPath      = */ "/system/bin/sleep",
                        /* cwd            = */ "/",
                        /* args           = */ arrayOf("sleep", "999999"),
                        /* env            = */ arrayOf<String>(),
                        /* transcriptRows = */ TRANSCRIPT_ROWS,
                        /* client         = */ sessionClient,
                    )

                    view.attachSession(session)

                    terminalView = view
                    terminalSession = session
                    view
                },
                update = { view ->
                    // Pull effective cols/rows off the emulator. Direct
                    // field access — both are public on TerminalEmulator.
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

        // --- Extra-keys bar + mic ---
        ExtraKeysBar(
            onKeyBytes = { bytes ->
                // Snap back to live screen on user input — every terminal
                // emulator does this so a keystroke after scrolling history
                // brings the prompt back into view.
                terminalView?.setTopRow(0)
                ws.sendBytes(bytes)
            },
            onScrollLines = { delta ->
                val v = terminalView ?: return@ExtraKeysBar
                val emu = v.mEmulator ?: return@ExtraKeysBar
                if (emu.isAlternateBufferActive) {
                    // Alt-screen TUI — forward PgUp/PgDn bytes to the app.
                    // Claude Code binds these to scroll its conversation
                    // view (it shows the hint "Use page up / page down to
                    // scroll"). Desktop keyboard PgUp doesn't work in
                    // gnome-terminal/iTerm2 only because those terminals
                    // intercept the keystroke for their own scrollback
                    // before forwarding to the app — we don't intercept,
                    // so the bytes reach Claude Code and it scrolls.
                    val seq: ByteArray = if (delta < 0) {
                        // PgUp = ESC[5~
                        byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '5'.code.toByte(), '~'.code.toByte()
                        )
                    } else {
                        // PgDn = ESC[6~
                        byteArrayOf(
                            0x1b, '['.code.toByte(),
                            '6'.code.toByte(), '~'.code.toByte()
                        )
                    }
                    ws.sendBytes(seq)
                } else {
                    // Main screen — scroll local transcript buffer.
                    // Bound: at most -activeTranscriptRows (deepest history),
                    //        at least 0 (live screen).
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
                        // Transcripts ship as bracketed-paste text frames
                        // — the server handles wrapping with ESC[200~ /
                        // ESC[201~ and emitting bytes back to the PTY.
                        ws.sendPaste(transcript)
                    },
                    api = api,
                    operator = operator,
                )
            },
            modifier = Modifier.fillMaxWidth(),
        )
    }
}

// ---------------------------------------------------------------------
// UI building blocks (private helpers)
// ---------------------------------------------------------------------

private enum class BannerKind { Info, Warn, Error }

@Composable
private fun TerminalTopBar(
    title: String,
    onBack: () -> Unit,
    onShowKeyboard: () -> Unit = {},
) {
    val feedback = rememberPressFeedback()
    Row(
        modifier = Modifier
            .fillMaxWidth()
            .background(BbxBlack)
            .padding(horizontal = 4.dp, vertical = 6.dp),
        verticalAlignment = Alignment.CenterVertically,
    ) {
        TextButton(onClick = { feedback(); onBack() }) {
            Text(
                text = "‹",
                color = BbxWhite,
                fontSize = 22.sp,
                fontWeight = FontWeight.Bold,
            )
        }
        Text(
            text = title.ifBlank { "Apps root" },
            color = BbxWhite,
            fontWeight = FontWeight.SemiBold,
            fontSize = 16.sp,
            modifier = Modifier
                .weight(1f, fill = true)
                .padding(start = 4.dp),
        )
        // Explicit "show keyboard" button — fallback when tap-on-terminal
        // doesn't pop the IME (e.g. focus already held elsewhere).
        TextButton(onClick = { feedback(); onShowKeyboard() }) {
            Text(
                text = "⌨",
                color = BbxWhite,
                fontSize = 20.sp,
                fontWeight = FontWeight.Bold,
            )
        }
    }
}

@Composable
private fun ReconnectBanner(text: String, kind: BannerKind) {
    val bg: Color = when (kind) {
        BannerKind.Info -> Neutral500.copy(alpha = 0.25f)
        BannerKind.Warn -> BbxAccent.copy(alpha = 0.18f)
        BannerKind.Error -> BbxAccent.copy(alpha = 0.28f)
    }
    val fg: Color = when (kind) {
        BannerKind.Info -> BbxWhite
        BannerKind.Warn -> BbxWhite
        BannerKind.Error -> BbxWhite
    }
    val glyph: String = when (kind) {
        BannerKind.Info -> "•"
        BannerKind.Warn -> "⚠"
        BannerKind.Error -> "⚠"
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
