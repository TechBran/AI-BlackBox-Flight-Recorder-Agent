# Android CLI Terminal Uplift — Fold Device-Validation Checklist

Branch `feat/android-cli-terminal-uplift`. Everything below is device-only-verifiable (Compose layout, gesture interception, real-zellij behavior) — unit tests can't cover it. Run on the Z Fold 6. Check each; note anything off.

## Sessions & launching (Tasks 1–4)
- [ ] Tap **claude** shortcut → a BRAND-NEW terminal spawns (not a resume of a prior session). Tap it again → ANOTHER new session. (The old "resume the same session" bug should be gone.)
- [ ] Spawn 3+ mixed-agent sessions (claude, gemini, codex, **grok**) via taps — each is fresh. Switch between them via the session switcher; each shows its own live terminal.
- [ ] **grok** shortcut launches the grok CLI (not an error, not a voice model).
- [ ] X (kill) button on a session removes it permanently; it's gone from the switcher.
- [ ] Open 12 sessions → the 13th tap toasts **"Session limit reached (12). Close a session (X) first."** and reads fully (LENGTH_LONG). Kill one → a new launch succeeds.

## YOLO (Task 2/4)
- [ ] The amber ⚡ button appears next to each agent shortcut (claude/gemini/codex/grok/antigravity) but NOT next to plain Terminal.
- [ ] ⚡ visibly renders as a tinted amber bolt (a vector, not a yellow system emoji), and looks visually distinct when enabled vs a launch in flight (disabled).
- [ ] Tapping ⚡ launches that agent with permissions skipped (e.g. claude runs `--dangerously-skip-permissions`, gemini `--yolo`, codex bypass, grok `--always-approve`). Confirm the agent is actually in skip-permissions mode.
- [ ] The session shows a persistent ⚡ badge in the switcher. **Kill the app and reopen** → the YOLO session still shows its ⚡ badge (rebuilt from the server list).
- [ ] The ⚡ button is comfortably tappable (48dp) — no fumbling to hit it, no accidental YOLO from aiming at the normal launch.

## Layout / rotation (Tasks 5–6)
- [ ] The terminal grid **fills the screen** — no wasted border margins around it (compare to before).
- [ ] The terminal's top rows are NOT hidden under the session switcher top bar.
- [ ] Rotate portrait→landscape **once** → the grid reflows and fully repaints to fit (NO "rotate twice" ritual, no content hanging off-screen). Rotate back → fits.
- [ ] Fold ↔ unfold → the grid re-fits in a single transition.
- [ ] Open the keyboard → the grid pushes up correctly (IME padding); close it → grid restores.
- [ ] The reconnect/reconnecting banner appears as an overlay on the top rows transiently and does NOT resize/shrink the grid when it shows or hides.
- [ ] (Reconcile) Rotate while briefly disconnected, then reconnect → the grid comes back at the correct current orientation.

## Input / scrollback / Esc (Task 7)
- [ ] In a running **claude** TUI, tap the screen to focus → keyboard appears, NO phantom characters spray into the prompt (including a slightly-draggy tap).
- [ ] Swipe-scroll works in a mouse-tracking claude TUI (scrolls history, no stray characters).
- [ ] **The plain-terminal→manual-claude repro:** open a plain Terminal, then manually run `claude` inside it → scrollback (swipe AND PgUp/PgDn buttons) works **identically** to a claude-launched session.
- [ ] **PgUp/PgDn buttons** scroll history in a running claude (not just swipe).
- [ ] **Esc key** on the extra-keys bar reliably interrupts/sends Escape in claude AND in a plain shell — including rapid taps and a slightly-draggy tap (this was the dead-key bug; Esc is now pinned in a fixed slot at the left, always visible).
- [ ] When no TUI is running (plain shell), tap-to-focus + typing still works normally.

## Fresh-box sanity (if testable)
- [ ] On a box without a grok binary, the grok launch fails gracefully (clear message / graceful in-pane "command not found"), not a crash.

---
When all green, this branch is device-proven. Any failure → note the exact repro and which item.
