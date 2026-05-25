# CLI Agent Terminal Architecture — Research & Options

> Status: research complete, decision pending. Brandon to review and pick a path.
> Date: 2026-05-24
> Author: Claude (deep-research agent + synthesis)
> Trigger: Today's debugging exposed how many provider-specific hacks our CLI Agent bridge has accumulated. Want a more reliable, provider-agnostic architecture.

## The smoking gun (one paragraph)

**We have a double-PTY layer that nobody else uses.** Our chain is `xterm.js → WebSocket → PtyBridge (PTY pair A) → tmux client → tmux server (PTY pair B) → CLI`. That's two PTYs and one Unix-socket hop between the browser and the CLI. Every terminal capability query (DA1, XTVERSION, DECRQM) has to traverse 5 boundaries round-trip — every boundary is a chance for bytes to be dropped, reordered, or rewritten. VS Code, Zellij, ttyd, terminado, WeTTY all use ONE PTY per session. The capability-negotiation hangs we hit on Claude almost certainly happen somewhere in that 5-boundary chain.

## The three options, in order of escalating change

### A. Polish what we have (1–2 weeks)

Keep the architecture; adopt the production techniques Zellij and VS Code have already debugged. Port their xterm.js shims into our `cli-agents-modal.js` and our `session_manager.py`:

- **Kitty keyboard protocol passthrough** — fixes Shift+PgUp, Ctrl+arrows, Alt+arrows for ALL providers, not just Claude
- **IME bypass** for fast typing (CJK + diacritics)
- **Mousemove hack** (xterm.js doesn't fire it natively; Zellij reaches into `term._core._mouseService`)
- **Server-side flow control** — track unacknowledged char count, pause PTY at high-water mark (VS Code pattern — prevents `cat largefile` floods)
- **tmux init-file for post-attach hooks** — instead of "wait 5s then send keys" race
- **xterm.js webgl + clipboard + web-links addons** (we're missing all three)
- **Reconnection state machine in Portal** with exponential backoff + cancel UI
- **`COLORTERM=truecolor` + `TERM_PROGRAM=ai-blackbox`** env vars (VS Code sets these)

**Resilience to provider changes:** medium. POST_ATTACH_HOOKS table still required.

### B. Drop the double-PTY (few days)

In addition to A: refactor `pty_bridge.py` so the PtyBridge IS the tmux client, not a wrapper around one. Removes a PTY layer entirely. Today's PtyBridge spawns `tmux attach -t <name>` inside a `python pty.fork()` — we can just have ptyprocess directly own that command without the bridge abstraction. Architecture becomes self-documenting.

**Resilience:** marginally better than A. Fewer moving parts.

### C. Adopt Zellij wholesale (3–4 weeks)

Replace tmux with [Zellij](https://github.com/zellij-org/zellij). Use Zellij's built-in web client (released 2025) as the Portal modal terminal. Our orchestrator becomes the auth/proxy/launcher; Zellij handles the rest.

```
Portal (our UI)
  ⇕ iframe
Zellij web client (zellij's xterm.js wiring, port 8082)
  ⇕ WebSocket
zellij-server (persistent, per-operator)
  ⇕ pty per pane
the CLI
```

**Free benefits:**
- Battle-tested xterm.js wiring (kitty keyboard, IME bypass, mouse, reconnect — all already shipped)
- Multi-client attach: power user `zellij attach https://.../<session>` from gnome-terminal sees the SAME session as the Portal user. **Exactly Brandon's mental model.** ✓
- Persistence across reboot
- Multi-pane, layouts, plugin system
- Bookmark-a-session-URL = come back to exact same state
- Read-only attach mode (for share-screen demos)

**Costs:**
- Ship a second binary + a second systemd service
- Portal modal becomes an iframe to localhost:8082; our branding is constrained
- Zellij's keybindings differ from tmux (Ctrl-p instead of Ctrl-b)
- Auth bridge needed to mint Zellij tokens from our operator config
- Zellij's web client is "young" (2025) — we'd be exposed to their bug pace

**Resilience to provider changes:** highest. Whatever Anthropic/Google/OpenAI changes in their CLI TUI, if it works in gnome-terminal it works in Zellij — because Zellij IS what gnome-terminal users would face.

## The technique we're missing entirely

**VS Code runs `@xterm/headless` server-side**, fed by every output byte, and uses the `serialize` addon to produce reflow-aware replay. On reconnect, the persisted replay is injected as `initialText` and the client sees the session resume exactly where it left off — at any window size.

We use `tmux capture-pane`, which is a snapshot at tmux's current width. If the reconnecting client has a different width, replay is wrong.

This is orthogonal to A/B/C — could be added on top of any. Cost is non-trivial (means introducing a Node sidecar or shipping `@xterm/headless` via npm in MCP venv).

## Recommended next move

1. **Spike Zellij first (1 day):** Install on a test machine, enable web client, launch `claude` in it, click around. Decide by *feel*, not docs. If Zellij's web client gives the experience Brandon wants — same UX as gnome-terminal, scrolling works on all providers, no hacks — then C becomes obvious.

2. **If Zellij feels right** → go straight to C, skip A and B. Effort spent on A/B becomes wasted when C replaces it.

3. **If Zellij feels wrong** (UI constraints, keybinding pain, immature web client) → do A + B together. Get most of the resilience benefit without the dependency on Zellij. ~2 weeks.

## Reference implementations to read

Order of value:

1. **[Zellij web client assets](https://github.com/zellij-org/zellij/tree/main/zellij-client/assets)** — `index.html` + `terminal.js` + `input.js` + `keyboard.js` + `connection.js` + `websockets.js` together are ~50 KB of code that solves our exact problem
2. **[VS Code `PtyService`](https://github.com/microsoft/vscode/blob/main/src/vs/platform/terminal/node/ptyService.ts)** — persistence, revival, flow control, replay
3. **[VS Code `TerminalProcess`](https://github.com/microsoft/vscode/blob/main/src/vs/platform/terminal/node/terminalProcess.ts)** — node-pty wiring, env, shutdown
4. **[VS Code `xtermTerminal`](https://github.com/microsoft/vscode/blob/main/src/vs/workbench/contrib/terminal/browser/xterm/xtermTerminal.ts)** — addon loading, compatibility shims
5. **[xterm.js VT features](https://xtermjs.org/docs/api/vtfeatures/)** — what xterm.js responds to natively (DA1 yes, XTVERSION no)
6. **[Claude Code issue #28077](https://github.com/anthropics/claude-code/issues/28077)** — why we need `/tui fullscreen`; Anthropic's stance on built-in scrollback

## Current implementation pointers (so future-self has the map)

- `Orchestrator/cli_agent/session_manager.py` — POST_ATTACH_HOOKS, env injection, tmux wiring
- `Orchestrator/cli_agent/pty_bridge.py` — select-based PTY read
- `Portal/modules/cli-agents-modal.js` — xterm.js singleton, WebSocket wiring

## Next decision

When Brandon comes back to this: pick A+B or run the Zellij spike. Either is a real path forward. Doing neither is also fine if today's friction is acceptable — the system DOES work today, just with provider-specific scaffolding.
