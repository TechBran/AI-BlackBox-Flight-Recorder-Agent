package com.aiblackbox.portal.ui.cli_agent

import com.termux.terminal.TerminalSession
import com.termux.view.TerminalView

/**
 * Resizes the session's emulator to [cols] x [rows], passing the cell size [view] renders at,
 * exactly as TerminalView.updateSize does. termux 0.118.1+ takes the cell size in pixels too:
 * it fills the pty's ws_xpixel/ws_ypixel and answers CSI 14t/16t pixel-size queries, so a
 * resize that passed 0 x 0 would wipe the size the view had already set. Before the view has
 * a renderer (no layout yet) the size is 0 x 0, as the emulator itself starts.
 */
internal fun TerminalSession.resizeLike(view: TerminalView?, cols: Int, rows: Int) {
    val renderer = view?.mRenderer
    updateSize(cols, rows, renderer?.fontWidth?.toInt() ?: 0, renderer?.fontLineSpacing ?: 0)
}
