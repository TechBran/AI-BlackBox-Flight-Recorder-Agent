package com.aiblackbox.portal.ui.cli_agent

import com.termux.terminal.TerminalEmulator
import com.termux.terminal.TerminalOutput
import com.termux.terminal.TerminalSession
import com.termux.terminal.TerminalSessionClient
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The CLI-agent terminal renders whatever the remote Claude/Gemini/Codex CLI prints, and that
 * includes text from web pages and files the agent read: hostile input. These pin that the
 * termux emulator we ship survives output that crashed or hung terminal-emulator v0.118.0
 * (audit X02-deps-android-web-08). ZellijTerminalScreen catches an append() exception and drops
 * the chunk, so a crash there silently corrupts the screen; a hang freezes the UI thread.
 *
 * TerminalEmulator is plain Java (the JNI half lives in TerminalSession), so this runs on the
 * JVM against the exact artifact the APK packages.
 */
class TerminalEmulatorHostileOutputTest {

    @Test(timeout = 20_000)
    fun `a cell flooded with combining marks does not overflow the row`() {
        // v0.118.0 kept every zero-width mark on the cell; past 32767 UTF-16 units the row's
        // short space counter wrapped and setChar threw ArrayIndexOutOfBoundsException.
        val flood = StringBuilder("a")
        repeat(20_000) { flood.appendCodePoint(0xE0100) } // VARIATION SELECTOR-17, zero width
        flood.append("b")
        val emulator = emulator()

        emulator.feed(flood.toString())

        assertEquals("the base char and the next char each take one column", 2, emulator.cursorCol)
        emulator.feed(CLEAR_AND_HOME + "ok")
        assertEquals("ok", emulator.row(0))
    }

    @Test(timeout = 20_000)
    fun `an absurd repeat count is capped instead of hanging the UI thread`() {
        // CSI Ps b (REP) repeats the last character Ps times. v0.118.0 looped two billion
        // times here; the VT510 cap of 9999 bounds it.
        val emulator = emulator()

        emulator.feed("x\u001b[2000000000b")

        assertTrue(emulator.row(ROWS - 1).startsWith("xxxx"))
        emulator.feed(CLEAR_AND_HOME + "ok")
        assertEquals("ok", emulator.row(0))
    }

    private fun emulator(): TerminalEmulator =
        TerminalEmulator(NullOutput, COLUMNS, ROWS, /* cellWidthPixels = */ 10, /* cellHeightPixels = */ 20, 100, NullClient)

    private fun TerminalEmulator.feed(text: String) {
        val bytes = text.toByteArray(Charsets.UTF_8)
        append(bytes, bytes.size)
    }

    private fun TerminalEmulator.row(row: Int): String =
        screen.getSelectedText(0, row, COLUMNS, row).trimEnd()

    private object NullOutput : TerminalOutput() {
        override fun write(data: ByteArray, offset: Int, count: Int) {}
        override fun titleChanged(oldTitle: String?, newTitle: String?) {}
        override fun onCopyTextToClipboard(text: String?) {}
        override fun onPasteTextFromClipboard() {}
        override fun onBell() {}
        override fun onColorsChanged() {}
    }

    private object NullClient : TerminalSessionClient {
        override fun onTextChanged(changedSession: TerminalSession) {}
        override fun onTitleChanged(changedSession: TerminalSession) {}
        override fun onSessionFinished(finishedSession: TerminalSession) {}
        override fun onCopyTextToClipboard(session: TerminalSession, text: String?) {}
        override fun onPasteTextFromClipboard(session: TerminalSession?) {}
        override fun onBell(session: TerminalSession) {}
        override fun onColorsChanged(session: TerminalSession) {}
        override fun onTerminalCursorStateChange(state: Boolean) {}
        override fun setTerminalShellPid(session: TerminalSession, pid: Int) {}
        override fun getTerminalCursorStyle(): Int = TerminalEmulator.DEFAULT_TERMINAL_CURSOR_STYLE
        override fun logError(tag: String?, message: String?) {}
        override fun logWarn(tag: String?, message: String?) {}
        override fun logInfo(tag: String?, message: String?) {}
        override fun logDebug(tag: String?, message: String?) {}
        override fun logVerbose(tag: String?, message: String?) {}
        override fun logStackTraceWithMessage(tag: String?, message: String?, e: Exception?) {}
        override fun logStackTrace(tag: String?, e: Exception?) {}
    }

    private companion object {
        const val COLUMNS = 80
        const val ROWS = 24
        const val CLEAR_AND_HOME = "\u001b[2J\u001b[H"
    }
}
