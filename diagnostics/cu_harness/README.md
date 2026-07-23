# CU click-accuracy & desktop-capability harness (M0)

Ground-truth verification for the Computer Use production-readiness plan
(`docs/plans/2026-07-23-cu-agent-production-readiness.md`). Two layers:

## 1. Unit gate (CI-safe, headless)

```bash
Scripts/cu-verify.sh
```

Runs the display-coherence, 413-guard, and desktop-population suites plus the
pre-existing CU unit tests. These encode the invariants:

- For a virtual session: screenshot display == click display == the handle's
  `:N`; the box-global `CU_NATIVE_MODE` can never stomp it back to `:0`.
- A virtual executor never routes input through ydotool (kernel-seat trap).
- Gemini 0-999 coords de-normalize against the session's own resolution.
- The Anthropic send loop budgets screenshots (keep-last-K) EVERY iteration.
- The populated desktop (tint2 + pcmanfm) spawns per session, degrades
  gracefully when packages are missing, and every role is torn down.

## 2. Live battery (on the box, real models, real displays)

```bash
Scripts/cu-verify.sh --live              # all three backends
Scripts/cu-verify.sh --live anthropic    # one backend
```

Per backend:

- **clicks** — `click_grid.py` (python3-tk) renders nine labeled crosshair
  targets (corners, edge midpoints, center) fullscreen on the agent's OWN
  virtual display and logs every ButtonPress with the distance to the nearest
  target center. The model is told to click each target; the runner prints a
  per-target PASS/FAIL table (tolerance 25px).
- **open-app** — the model is told to open a terminal from the populated
  desktop (right-click Applications menu). `xdotool search --class xterm` on
  the session display is the ground truth.

Requires: the Orchestrator running on `localhost:9091` (override with
`BLACKBOX_URL`), `python3-tk`, `xdotool`, and API keys for the backends under
test. Sessions run under the `cu-harness` operator; close leftovers via
`/cu/session/{sid}/close` or let the idle reaper collect them.

### Reading a failure

- `never clicked` on left/right column targets, accurate center → aspect/scale
  distortion (the anisotropic-squish class).
- Uniform offset on ALL targets → wrong display captured vs. clicked
  (coherence class) — check `[CU-VIEW]`/`[CU-BG]` journal lines.
- Model reports success but zero clicks logged → input landing on another
  display (the `:0` stomp class) or ydotool routing.
