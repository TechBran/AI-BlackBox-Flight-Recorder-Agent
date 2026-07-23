# CU Agent Production-Readiness — Display Coherence, Populated Desktop, Click Accuracy, 413

## Context

Computer-Use agents regressed: they can "see" a desktop but their clicks don't
accomplish tasks, the spawned desktop "only shows a browser" with no way to open
apps/folders, and Anthropic dies with an error Brandon paraphrased as "too much
to do." A 41-agent Opus investigation (code + **live MS02 forensics**) proved the
true causes — and they are not what the symptoms suggested:

1. **The agent is driving Brandon's REAL desktop (`:0`), not a spawned one.**
   `CU_NATIVE_MODE` defaults `True` (config.py:208) and MS02 has no override, so
   `ACTIVE_DISPLAY` = the real GNOME `:0` (3440×1440). Live journal for Brandon's
   run (PID 783267) shows the model screenshotting `:0` (his crown wallpaper +
   folders) and repeatedly double-clicking `[1163,141]` with no result. This is
   both the bug **and a safety hazard** (an agent clicking around the operator's
   real desktop). The per-session virtual displays (`:100+`) the architecture is
   *supposed* to use are short-circuited: `session_manager.py:204` `if
   self.native_mode: return True` returns before allocating one.
2. **Two live-view surfaces show different displays.** `/cu/view/main` streams the
   real `:0`; `/cu/view/{sid}` streams the virtual `:99/:100` (browser-only). The
   human watches one display while the agent may act on another — the source of
   Brandon's contradictory experience.
3. **The virtual desktop is barren.** `DisplayAllocator._start_quartet`
   (display.py:208) spawns only Xvfb + openbox (empty rc) + x11vnc + websockify —
   **no panel, launcher, file manager, or desktop icons**. Chrome is the only app
   that ever appears (launched separately). openbox alone = a blank managed root.
4. **Anthropic 413 `request_too_large` (root cause, certain).** The per-step CU
   loop re-sends the ENTIRE accumulated history every iteration and never strips
   screenshots mid-run — `strip_screenshots_from_history` is called only at
   task-save (driver_anthropic.py:552), never inside the send loop. With
   `CU_MAX_ITERATIONS=150`, up to ~150 full-res PNGs accumulate, blowing past
   Anthropic's 100-image / ~32MB request caps. Native `:0` at 3440×1440 makes each
   PNG multi-MB, so it dies fast (Brandon's task died at step 10). **This is the
   "too much to do" error — a payload-size failure, not a task-complexity limit.**
5. **Anisotropic screenshot squish (contributing).** In native mode the 3440×1440
   (2.389:1) frame is force-resized to 1280×720 (1.778:1) with no aspect
   preservation — 25.6% horizontal compression every frame — so all models ground
   on a distorted image and mis-target on wide displays. (The coord *transform* is
   mathematically exact; the *image* is distorted.)
6. **Per-model gaps.** Gemini's click path is never threaded to a per-session
   display (`ActionExecutor(coord_space=COORD_SPACE_GEMINI)` with no
   `display_number`, gemini_cu). OpenAI sends a bare `[{'type':'computer'}]` tool
   with **no** `environment` / `display_width` / `display_height` (the "flag to
   jump into the desktop" Brandon mentioned is absent).

**The convergence that makes this tractable:** the per-session virtual display is
**already** each model's native resolution — Anthropic/OpenAI 1280×720, Gemini
1440×900 (`display.py:resolution_for_backend`). So making agents *always* run on a
**populated virtual desktop** simultaneously: gives coherent screenshot=click=
stream on ONE display, makes scale = 1.0 (kills the aspect distortion + all the
scaling math), and shrinks screenshots ~10× (relieves the 413). One architectural
decision fixes causes 1, 2, 5, and de-risks 4.

**Decisions (Brandon, this session):** (a) **Dedicated virtual desktops only** —
every agent gets its own populated virtual display; agents NEVER touch `:0`; the
real desktop stays human-only (still viewable/controllable via the existing
main-desktop live view for manual "jump in myself" use). (b) **Lightweight DE**
(openbox + tint2 + pcmanfm + xterm). (c) **All three models** fixed + validated.

---

## Guiding principle

**One per-session `DisplayHandle` is the single display authority.** Every
screenshot, every click, every coordinate scale, the Chrome/app spawn, AND the
human live-view all bind to the *same* handle. No code path reads the module
globals `ACTIVE_DISPLAY` / `NATIVE_MODE` to pick a display for an agent again.
Agents are always virtual; `:0` is never an agent surface.

---

## Milestones (TEST-FIRST — the harness lands before the fixes, per Brandon)

### M0 — Ground-truth click-accuracy test harness (BUILD FIRST)

The acceptance gate. Must be able to FAIL against today's code and PASS after the
fix, per model, deterministically.

- **Click-precision test** (`Orchestrator/tests/cu/test_click_accuracy.py` +
  a live harness runnable on MS02): spawn a real per-session virtual desktop, put
  known targets at known pixel coordinates on it, drive each backend to click a
  named target, and assert the click landed within tolerance.
  - Target surface: a tiny always-on-top Tk/xterm "click grid" app (or an
    `feh`-displayed generated PNG with labeled buttons at known px) launched on the
    session's `:N`. A click logger records where the pointer actually went
    (`xdotool getmouselocation` sampled around the action, or an X client that
    logs `ButtonPress` coordinates).
  - Assertion: `|actual − target| ≤ tolerance` (e.g. ≤10px) for a battery of
    targets across the frame (corners + center + edges — the anisotropic-squish
    failure shows up at the horizontal extremes).
  - Runs per backend: Anthropic, OpenAI, Gemini.
- **Open-an-app E2E** (`test_cu_open_app.py`): instruct each model "open the file
  manager and open the Home folder"; assert a pcmanfm window appears
  (`wmctrl -l` / `xdotool search`). Proves the populated desktop + real task
  capability, not just raw clicks.
- **Display-coherence unit tests** (pure, fast): assert that for a virtual session
  the screenshot display == the click display == the streamed display == the
  handle's `display_num`, and that NO agent path resolves to `:0`.
- **413-guard unit test**: build a synthetic 150-iteration history and assert the
  outgoing request carries ≤ N images and ≤ a byte budget.

Deliverable: `make cu-verify` (or a documented script) that runs the whole battery
against a chosen backend on MS02 and prints a per-target pass/fail table.

### M1 — Single display authority (the core fix)

- Promote `DisplayHandle` to the one per-session display object threaded
  **explicitly** into every capture + click + scale call. Remove the
  `if NATIVE_MODE: env = get_native_env()` short-circuits from the primitives
  (`actions.py:_run_xdotool` ~:142-147, `screenshot.py:capture_screenshot_display`
  ~:104-108) so a passed `display_number` is always honored.
- Scaling: derive scale from **the handle's own resolution**, not
  `detect_native_resolution()` (the real `:0`). For a native-res virtual display
  scale = 1.0 (`ActionExecutor._scale_coord`, config.get_scale_factors).
- Make agents **always virtual**: the interactive chat CU paths and the
  `use_computer`/`/browser/run` task paths allocate + bind a per-session handle;
  the `native_mode=True` agent branch is removed as a default. Native `:0` driving
  is retained ONLY as an explicit, arbiter-guarded, non-default opt-in (or removed
  entirely for agents — see M1 note). `/cu/session/open` + the agent launch bind
  the SAME handle so the human live-view and the agent act on one display.
- Files: `Orchestrator/browser/{actions,screenshot,session_manager,dispatch,
  headless,driver_anthropic}.py`, `Orchestrator/gemini_cu/*`,
  `Orchestrator/openai_cu/*`, `Orchestrator/routes/browser_routes.py`, the chat CU
  dispatch in `Orchestrator/routes/chat_routes.py`.

### M2 — Populate the virtual desktop (lightweight DE)

- Extend `_start_quartet` (display.py) to also spawn, on the session `:N`:
  `tint2` (panel + app launcher), `pcmanfm --desktop` (desktop icons + file
  manager), and keep `xterm` available; ship a minimal `tint2rc` + pcmanfm profile
  as repo assets (`Orchestrator/browser/assets/`). Give openbox a real
  right-click menu (jgmenu or a populated `menu.xml`).
- Add the new roles to `release()`'s teardown tuple (else they leak on reap).
- Install packages on MS02 + add to the installer package list
  (`Scripts/onboarding/system-packages.txt`): `tint2 pcmanfm xterm` (jgmenu/feh
  optional). All apt-available; confirmed absent on MS02 today.
- Confirm x11vnc streams the populated desktop cleanly (it will — same X server).

### M3 — Fix the Anthropic 413

- Strip screenshots INSIDE the send loop: keep only the last K image tool_results
  (e.g. K=3), replacing older ones with a text placeholder ("[screenshot N
  elided]"), every iteration before the API call — reuse
  `strip_screenshots_from_history` logic but apply per-turn with a keep-window.
- Belt-and-suspenders: cap images per request and resize each sent frame to the
  model's native dims (already 1280×720 on a virtual display → cheap). Optionally
  lower `CU_MAX_ITERATIONS` default or add a hard image-count budget.
- Files: `driver_anthropic.py` (the send loop), `session_manager.py`
  (`strip_screenshots_from_history` gains a keep-last-K variant).

### M4 — Per-model correctness

- **Anthropic:** set the `computer` tool's `display_width_px/height_px` to the
  session resolution; confirm tool version matches the model.
- **OpenAI:** send `environment: "linux"` (ubuntu) + `display_width/display_height`
  = session resolution so gpt-5.x grounds on a full desktop, and thread the click
  executor to the session display.
- **Gemini:** thread the per-session `display_number` into the
  `ActionExecutor(coord_space=COORD_SPACE_GEMINI)` construction; de-normalize
  0–1000 coords against the **session** resolution (1440×900), and send sharp
  native frames (already correct per 0c15d2c8, now on the right display).

### M5 — Recent-commit regression audit + docs

- Audit `1af795fb` (virtual-default intent vs. `native_mode` fallback=True
  contradiction), `89fc56e5` (inert threading), `a3233c8b` ("pill Live opens the
  NATIVE screen"), `0c15d2c8`/`04b04d44` (Gemini native-frame fixes that entrenched
  the single-global assumption). Document what each did vs. the corrected model.
- Fix the manual live-viewer endpoints (`/browser/click|type|key|scroll`,
  interaction.py) to be session-aware (currently hardwired to the `:0` singleton).

---

## Verification (end-to-end, on MS02)

1. `make cu-verify` (M0 harness) green for all three backends: click-precision
   within tolerance across corners+center+edges; open-app E2E spawns pcmanfm and
   opens Home.
2. Live watch: `/cu/view/{sid}` shows the SAME populated desktop the model acts on
   (panel + icons + file manager), and the model opens an app/folder on it.
3. Confirm `:0` is untouched during an agent run (`DISPLAY=:0 xdotool
   getmouselocation` does not move while the agent works; agent pointer moves only
   on `:N`).
4. Long-task Anthropic run (>20 steps) completes with NO 413; inspect request
   image count stays ≤ K.
5. Unit suite (display-coherence, 413-guard, scaling=1.0-on-virtual) green in CI
   (`Orchestrator/tests/cu/`).

## Non-goals / guardrails

- No history rewrite of the ledger; additive only.
- Agents never drive `:0` (safety); the human's manual `:0` control via the
  main-desktop live view is unchanged.
- Keep the Splashtop live-view client as-is (just ensure it streams the agent's
  populated `:N`).

---

## M5 — Recent-commit regression audit (completed 2026-07-23)

The regression was a SPLIT-BRAIN introduced across two 2026-07-21 commits:

- **1af795fb** (virtual-by-default launches): flipped the LAUNCH layer to
  per-session virtual displays — but left the input/capture PRIMITIVES
  consulting the box-global `NATIVE_MODE`. On a `CU_NATIVE_MODE=True` box
  (the default; MS02) sessions allocated `:100+` displays while agents still
  screenshotted and clicked `:0`. This is the commit that created the bug
  Brandon hit.
- **89fc56e5** (thread display through executor/capture): added the RIGHT
  wiring, but it was inert on native boxes — everything it threaded was
  overridden downstream by the `if NATIVE_MODE:` short-circuits in
  `_run_xdotool` and `capture_screenshot_display`. Its tests passed only
  because they monkeypatched `NATIVE_MODE=False` (the test file's own
  adaptation note admits the dev box runs native — the tell that was missed).
- **a3233c8b** (pill Live opens the NATIVE screen): UI-level routing that,
  combined with the above, produced the two-surfaces confusion — the human
  watched one display while the agent acted on another.
- **0c15d2c8 / 04b04d44** (Gemini sharp native frames + grid-grounding rule):
  correct fixes in the single-global-display world, but they entrenched the
  `display_number -> ACTIVE_DISPLAY` fallback and bare `ActionExecutor`
  constructions. `ensure_display()` now binds those paths per-session.

**The corrected model (shipped with this plan):** the `native` decision is an
EXPLICIT per-call/per-executor parameter threaded from the session's own
state; the box-global `NATIVE_MODE` is only a fallback for legacy/manual
native-desktop paths. A `native_mode=False` executor/capture can never be
stomped back to `:0`, and virtual executors never route input through ydotool
(kernel-seat injection cannot reach an Xvfb display).
