# CU Live Panel — Splashtop-Style Remote-Desktop Revamp (Design)

**Date:** 2026-07-23
**Author:** Design pass (Brandon directive 2026-07-23; Splashtop 2 is the explicit UX roadmap)
**Status:** DESIGN — for review before implementation plan
**Predecessor:** `docs/plans/2026-07-20-local-model-stack-design.md` §9 (CU virtual displays, D6/D11/D14) — this doc answers its open question Q5 ("live-view interactivity: view-only or takeover?").

---

## 1. Goal & Invariant

Turn the CU live panel — across all three frontends (Portal web, Android MVP, WebView wrapper) — into a real remote-desktop client over each CU session's **existing native-resolution virtual display**:

1. **Live stream** of the desktop (damage-driven, ~realtime), replacing 1.5s/2s JPEG screenshot polling.
2. **Touchpad-style draggable cursor** (indirect/relative pointer) that performs the clicks — freeing direct touch for:
3. **Pinch-to-zoom + pan** of the native-res display (1280x720 / 1440x900) on small screens.
4. **Extra-keys bar** (Esc/Tab/Ctrl/Alt/Super/arrows/Fn, selectable, sticky modifiers).
5. **Manual keyboard open** via a button — kill open-keyboard-on-every-tap.

### The CU-agents-untouched invariant

**CU agents themselves are not modified**: their per-session displays and resolutions (`Orchestrator/browser/display.py:33-40` — `resolution_for_backend`: gemini 1440x900, anthropic/openai 1280x720), their screenshot capture (scrot per `display_num`), their input injection (`ActionExecutor`, `Orchestrator/browser/actions.py:206-221`), and their tools all stay as-is. The revamp is a **viewer-side** upgrade: the agent's capture/injection paths are independent of the viewing path by construction (agents never read the VNC stream; the VNC server reads the same Xvfb the agent drives).

One caveat is called out honestly in §5 and §10-D4: for OpenAI and Gemini backends a **pre-existing display-binding bug** means the display the agent *acts on* is not always the display you'd *watch*. Fixing that is a bug fix to driver plumbing (not agent semantics, resolutions, or tools) and is a scoping decision, not silently in-scope.

---

## 2. What Exists Today (grounding)

Two disjoint viewing paths exist; the revamp is essentially "merge them and add input to the streaming one."

### 2.1 The true live stream (watch-only)

Every virtual CU session already gets a full per-session quartet (`Orchestrator/browser/display.py:199-230`, `_start_quartet`):

- **Xvfb** `:100+slot` at the backend's native resolution, depth 24 (`display.py:202-206`)
- **openbox** WM (`display.py:213-215`)
- **x11vnc** `-forever -shared -nopw -listen 127.0.0.1 -rfbport {5901+slot} -noxdamage` (`display.py:218-221`) — note: **no `-viewonly` flag; the server accepts RFB input today**
- **websockify** `127.0.0.1:{6101+slot}` → the RFB port, spawned only when the `websockify` binary and `/usr/share/novnc` both exist (`display.py:43-46` `_live_view_available()`, `display.py:223-227`) → sets `handle.live_view`

Slots: cap 3 concurrent, 1800s idle TTL reaper, pid-scoped teardown (`display.py:23-27`, `release()` at `display.py:236-257`).

The Orchestrator fronts this with:

- `GET /cu/sessions` → `{active, count, cap, sessions:[to_public]}` (`Orchestrator/routes/browser_routes.py:216-224`; payload shape `display.py:to_public` ~:148-159: `session_id, operator, backend, width, height, display, live_view, view_url, started_at`)
- `GET /cu/view/{session_id}` → **inline** noVNC viewer HTML `_CU_VIEW_HTML` (`browser_routes.py:227-240`) with `rfb.viewOnly = true` (D11), `rfb.scaleViewport = true`, `rfb.resizeSession = false`; 404 for unknown session; "install novnc + websockify" fallback page when `live_view=False` (`browser_routes.py:242-258`)
- `WS /cu/view/{session_id}/ws` → transparent bidirectional binary proxy to the session's loopback websockify (`browser_routes.py:261-330`), mirroring the proven `app_proxy_websocket` pattern (`Orchestrator/routes/agent_routes.py:1482`); its docstring codifies "the Tailscale perimeter is the auth boundary (§9)"
- noVNC assets mounted at `/cu/novnc` from `/usr/share/novnc` (conditional; `Orchestrator/app.py:~222-229`)

Consumers: Portal `cu-live-view.js` polls `/cu/sessions` every 4s (`Portal/modules/cu-live-view.js:6,12-18`), shows a "● N agents running — watch" pill, and iframes `view_url` into `#cuLiveViewPanel` (`cu-live-view.js:40-47`; markup `Portal/index.html:1308-1316`, labeled "watch-only"; hard-opens `sessions[0]` at `cu-live-view.js:30`). Android has a complete but **unwired** `CuLiveViewScreen.kt` (WebView on the same URL, `ui/cu/CuLiveViewScreen.kt:16-36`) and `CuSessionsClient` — dead code, no NavGraph route.

### 2.2 The interactive path (screenshot polling, wrong display in virtual mode)

- Portal `cu-interact.js`: fullscreen modal, 1.5s polling of `GET /browser/screenshot/live` (`Portal/modules/cu-interact.js:15` POLL_MS, `:223-244` refresh) — each poll is scrot → PIL JPEG q70 → **a disk write** to `Portal/uploads/browser_live_<ts>.jpg` + a second round-trip to fetch it (`browser_routes.py:162-205`, keeps last 5 files). Direct tap = scaled click via `POST /browser/click` (`cu-interact.js:246-264`); wheel → `/browser/scroll`; keys → `/browser/key` / `/browser/type`. Quick-key buttons **re-focus the typing input to keep the soft keyboard open** (`cu-interact.js:132`). Resolution fetched **once** from `/browser/status` (`cu-interact.js:30-53`), fallback 1280x720.
- Backend: `/browser/click|type|key|scroll` (`browser_routes.py:108-159`) delegate to `Orchestrator/browser/interaction.py`, whose **module-level singleton** `_EXECUTOR = ActionExecutor()` (`interaction.py:22`) targets `ACTIVE_DISPLAY` — the native display or legacy `:99` (`Orchestrator/browser/config.py:76-83`) — **never** the per-session `:100-:102`. `_scale_xy` clamps to global `DISPLAY_WIDTH/HEIGHT` (`interaction.py:26-32`). `/browser/key` has a `[a-zA-Z0-9+_]` charset allowlist (`interaction.py:56-59`). **None of the four endpoints accept a `session_id`.** In virtual mode this viewer screenshots and clicks a display no CU session is on.
- Android `CuScreen.kt` (mounted at `NavGraph.kt:194`): 2s screenshot-byte polling (`:132, :321-334, :444-480`); the ViewerArea tap handler does `viewModel.click(x, y)` **and** `showTypingInput = true` on every tap (`CuScreen.kt:~693-701`), and `CuTypingInput` auto-focuses via `LaunchedEffect { focusRequester.requestFocus() }` (`CuScreen.kt:~1495-1500`) — **the exact keyboard-on-every-tap behavior to kill**. Quick keys: Return/Tab/BackSpace/Esc + scroll up/down only (`CuScreen.kt:~1418-1447`). `ContentScale.FillBounds` + `aspectRatio`, no zoom/pan/drag/cursor.
- WebView wrapper (`PortalActivity.kt:76,487`) inherits the Portal web behavior verbatim.

### 2.3 Environment

- **Dev box:** x11vnc 0.9.16 ✓, Xvfb ✓, xdotool ✓, X11 session; **websockify ✗, novnc ✗** → `live_view=False` on every session, `/cu/view` serves the fallback page. Both packages are already declared in `Scripts/onboarding/system-packages.txt:26-27` (SHOULD_HAVE) — just never applied here.
- **MS02** (`bbx@192.168.1.153`): websockify 0.10.0 ✓, novnc 1.3.0 ✓ (`/usr/share/novnc/core/rfb.js` confirmed), x11vnc ✓, Xvfb ✓ — fully provisioned.

---

## 3. Chosen Streaming Architecture

### 3.1 Decision: per-session x11vnc → Orchestrator WS proxy → noVNC-derived interactive client

**Reuse the shipped pipeline and unlock it.** The stream transport is unchanged: per-session `x11vnc` (loopback) → `websockify` (loopback) → `WS /cu/view/{sid}/ws` (Orchestrator transparent proxy) → RFB client in the panel. The changes are all at the edges:

1. **Promote the viewer from an inline Python string to a real served asset.** `_CU_VIEW_HTML` (`browser_routes.py:227-240`) becomes `Portal/cu-view/index.html` + `Portal/cu-view/cu-view.js` (served by `GET /cu/view/{session_id}` with the session id/params templated or passed via query/fetch of `/cu/sessions`). An inline string cannot host a gesture layer, cursor overlay, extra-keys bar, and reconnect logic.
2. **Flip `rfb.viewOnly` off** (revisiting D11 — see §6 security and §5 arbitration). x11vnc already accepts input (`display.py:218-221` has no `-viewonly`), so pointer/key events flow into the correct per-session Xvfb **with zero backend change** — this also completely sidesteps the `interaction.py` global-display bug for virtual sessions.
3. **Build the Splashtop UX as a JS layer over the RFB canvas** (§4): touch is intercepted *before* noVNC's default direct-tap handling; pointer/key events are sent programmatically through the RFB API.
4. **One shared client for all three surfaces**: the Portal opens `/cu/view/{sid}` in its panel iframe (as today), Android's `CuLiveViewScreen` WebView loads the same URL (`CuLiveViewScreen.kt:29`), and the WebView wrapper gets it through the Portal. One touch-first page ships the three-surfaces rule in one codebase.

### 3.2 Why this beats the alternatives

| Option | Verdict | Why |
|---|---|---|
| **(a) x11vnc + websockify + noVNC, interactive** | **CHOSEN** | ~90% already shipped and proven on MS02. Damage-driven updates (real stream, not frames-on-a-timer). Input is session-correct *by construction* (RFB → x11vnc → that session's Xvfb). Install cost on the dev box: `apt install websockify novnc`. Boring, proven, fits the box. |
| (b) Native RFB client on Android (AndroidVNC-class lib) | Rejected for v1 | Duplicates the client per surface, breaks the one-page parity trick, and the WebView path already works (`CuLiveViewScreen.kt` exists). Revisit only if WebView gesture latency proves unacceptable on the Fold. |
| (c) MJPEG/PNG-over-WS frame pump | Rejected | Reinvents transport at 1-4 fps class latency, much higher bandwidth at 1440x900, still needs the per-session input routing fixed separately. Only justified if VNC packages were prohibited — they're already in `system-packages.txt`. |
| (d) WebRTC (GStreamer `webrtcbin`) | Rejected | Lowest latency in theory, but needs plugins-bad + libnice (unverified), a signaling channel, H.264 plumbing, and a separate input channel. Heaviest build; redundant over Tailscale LAN-grade links. ffmpeg CLI is absent on both boxes, killing ffmpeg-based variants too. |

### 3.3 Stream lifecycle

Unchanged and already correct — the stream **is** the session:

- x11vnc + websockify start inside `DisplayAllocator.allocate()` → `_start_quartet()` when the CU session starts (`Orchestrator/browser/session_manager.py:203-224` `ensure_browser` for anthropic/openai), and die in `release()` on session stop / headless finally / 1800s TTL reap / boot orphan reap (`display.py:236-257`, `startup.py:~209-248`).
- `x11vnc -forever` survives viewer disconnects; viewers attach/detach freely mid-session.
- **New: an ended-session state.** Today `/cu/view/{sid}` 404s after TTL-reap and a mid-watch client just gets a dead iframe (`browser_routes.py:252-255`; WS closes 1008). The new client must handle WS close + `/cu/sessions` disappearance with a "session ended" card and a session-switcher back-link, plus bounded auto-reconnect (for Orchestrator restarts, where sessions can survive as orphans until reaped).

### 3.4 Multi-viewer & multi-session

- **Multi-viewer:** `x11vnc -shared` (`display.py:219`) already permits N concurrent RFB clients per session; each gets its own WS proxy connection. No change. All connected viewers' input merges at the X server — acceptable inside the trust model (§6); the panel shows a subtle "input is live" indicator.
- **Multi-session (cap 3):** the panel gets a **session switcher** fed by `/cu/sessions` (which already carries everything needed: backend, WxH, operator, live_view). Fixes `cu-live-view.js:30` hard-opening `sessions[0]`.

### 3.5 Native-mode sessions

Native mode (real physical desktop, arbiter-serialized via `display_arbiter.py`) has **no x11vnc** attached. **Scope decision: the streaming revamp targets virtual sessions only** (which are the default for CU tasks). Native-mode continues on the existing screenshot-poll interactive path — which is *correct* for native mode, since `interaction.py`'s `ACTIVE_DISPLAY`/scaling logic was built for exactly that case (`interaction.py:26-32`). This also gives the retained-fallback story for free (§8). Attaching an on-demand x11vnc to the real X server is listed as future work (§10-D6).

---

## 4. Interaction Layer Spec (the Splashtop UX)

All of this lives in the new served viewer page (`Portal/cu-view/`), running identically in the Portal iframe, Android WebView, and wrapper. Touch handling is registered on an overlay div **above** the RFB canvas with `touch-action: none`, so noVNC's default direct-tap-to-click mapping never sees raw touches. Mouse (desktop web) can keep direct mode — the touchpad layer activates on coarse pointers (`pointer: coarse` media query) with a manual toggle either way.

### 4.1 Touchpad-mode draggable cursor (indirect pointer)

- A **client-side virtual cursor** (SVG arrow, high-contrast with outline — remember the Android arrow black-on-black lesson) rendered at a position in *display space* (native px of the session, from `/cu/sessions` width/height).
- **One-finger drag** = relative cursor movement (Splashtop touchpad semantics): `Δcursor = Δtouch / zoomScale` so cursor speed feels constant at any zoom. Movement streams `pointerEvent(x, y, mask=0)` over RFB (hover works — menus, tooltips).
- **Tap** (short, < ~10px movement) = **left click at the cursor's current position** — not at the tap position. Sent as RFB button-down/up at cursor coords.
- **Double-tap** = double click. **Tap-then-drag** (tap, then immediately drag within ~300ms) = press-and-hold drag (button held during movement) — this is how the user drags windows/selects text.
- **Two-finger tap** = right click at cursor position.
- **Two-finger drag (parallel)** = scroll wheel events at cursor position (RFB buttons 4/5), with accumulator → discrete wheel ticks.
- Cursor position clamps to `[0, width]×[0, height]`; auto-pans the viewport when the cursor is dragged against a zoomed-in edge (Splashtop edge-push behavior).
- Input channel: `RFB._sendPointerEvent`-equivalent — see §10-D2 on the exact API (noVNC 1.5.x exposes cleaner seams than the apt 1.3.0; another argument for vendoring).

### 4.2 Pinch-zoom + pan

- The RFB canvas is wrapped in a transform container; **`rfb.scaleViewport` is turned OFF** (it currently shrinks 1440x900 unreadably on phones — `browser_routes.py:238`) and replaced with a client-managed CSS transform: `scale` ∈ [fit-to-screen … 3×], `translate` for pan.
- **Pinch** (two fingers, distance change) = zoom about the pinch centroid. **Two-finger drag** while zoomed = pan. (Disambiguation between two-finger-scroll and pan: scroll when at fit-zoom, pan when zoomed in; a mode indicator + long-tunable thresholds — validate on the Fold.)
- `rfb.resizeSession` stays **false** always — the invariant: we never resize the agent's screen (D6).
- Double-tap with two fingers (or a toolbar button) = reset to fit.

### 4.3 Extra-keys bar

Clone the shipped patterns: web `Portal/modules/cli-agents-extra-keys.js` (pinned-Esc-outside-scroll-strip layout, `preventDefault` on pointerdown so buttons never steal focus/IME, sticky-Shift tap=armed/long-press=locked — all documented in its header comment) and the Android terminal extra-keys bar (commit `7f83f461`).

- **Row 1 (modifiers + core):** `Esc` (pinned) | `Tab` `Ctrl` `Alt` `Super` `Shift` — modifiers are **sticky**: tap = one-shot arm (consumed by next key), long-press = locked until tapped again; visual armed/locked states.
- **Row 2 (nav):** `↑ ↓ ← →` `Home` `End` `PgUp` `PgDn` `Del` `⏎` `⌫`.
- **Row 3 (Fn, collapsible):** `F1`–`F12`.
- Rows/keys are **selectable** (settings popover, persisted in `localStorage`), mirroring the "selectable" requirement.
- **Delivery: RFB keysym events** (`rfb.sendKey(keysym, code, down)`), *not* `POST /browser/key` — the REST endpoint targets the wrong display for virtual sessions and its `[a-zA-Z0-9+_]` allowlist (`interaction.py:56-59`) can't express arrows/F-keys/Super anyway. Sticky modifiers = send modifier keydown, then the key's down/up, then modifier keyup.

### 4.4 Manual keyboard toggle + killing open-on-tap

- A **keyboard button** in the toolbar/extra-keys bar toggles a hidden input element (`inputmode` tuned, `autocapitalize=off`) that raises the soft keyboard **only when tapped**; its input/keydown events are forwarded as RFB key events (text via per-char keysyms; IME composition committed on input).
- **Remove today's auto-open behavior at its three homes:**
  - Android `CuScreen.kt`: delete `showTypingInput = true` from the tap gesture handler (`CuScreen.kt:~700`) and the `LaunchedEffect { focusRequester.requestFocus() }` auto-focus in `CuTypingInput` (`CuScreen.kt:~1495-1500`); add a manual keyboard button next to `CuQuickActions`. (This applies to CuScreen *while it remains the fallback* — the primary Android path becomes the WebView client, §7.2.)
  - Portal `cu-interact.js`: drop the `typingInput.focus()` re-focus in the quick-key handler (`cu-interact.js:132`) — again, fallback-path hygiene.
  - New viewer: the RFB canvas / overlay must never be focusable into an IME (`inputmode="none"` on any focus sink; noVNC's own keyboard-input element kept unfocused unless the toggle is on).

### 4.5 Status & narration overlay

Keep the existing dual narration feed and surface it *inside* the panel additively: SSE `cu_*` events (`Portal/modules/chat-send.js:1248-1455`; Android `ChatViewModel.kt:2318-2380`) for in-chat, and `/tasks/{id}` `progress_text`/`reasoning_text` (rendered textContent-only, `Portal/modules/ui-setup.js:658-700`) for pills. The panel adds a thin top bar: session backend/resolution, agent step (from `heartbeat {step,total}`), the E-stop (wired to the existing `/chat/cu-stop` path used by the drawer, `Portal/modules/cu-drawer.js:305-344`), and the "agent is acting" indicator (§5).

---

## 5. Input Arbitration: Human vs Agent on One Display

With `viewOnly` off, user RFB input and agent `ActionExecutor` input (xdotool, `actions.py:142-150`) merge at the same Xvfb X server, unarbitrated — `display_arbiter.py` is **native-mode-only** by design (`display_arbiter.py:171-184`) and has no say inside a virtual session.

**Decision: etiquette + soft guard, no hard lock (v1).**

- **Etiquette (documented in the panel's help):** the panel is for *assist and rescue* — dismissing a popup, typing a password the agent shouldn't see, unsticking a state — not for co-driving. If you want to take over, hit **E-stop** first (already per-session via the drawer / `cu_stopped`), then drive freely.
- **Soft guard (worth adding, cheap):** the panel shows a prominent **"agent acting…"** state driven by data it already receives — SSE `cu_action` events (`driver_anthropic.py:253`; forwarded at `chat-send.js:1294`) and the `heartbeat` step counter — and dims/haptics the touchpad layer for ~1.5s around agent actions. This is client-side only: no agent change, no server change, honest degradation when SSE isn't connected.
- **Explicitly rejected for v1:** a server-side per-session input mutex (pause-agent-while-user-drives). It would require touching the drivers' action loops — violating the agents-untouched invariant — for a contention case the E-stop already handles. Reconsider post-v1 if real usage shows fights (see §10-D5).
- The agent is unaffected by observation: screenshots come from scrot on its display, not the VNC stream; x11vnc is a read-side attach plus injected events indistinguishable from xdotool's at X level.

---

## 6. Security Stance

**Tailscale (+LAN) is the perimeter — unchanged.** This is codified in memory (`tailscale_security_perimeter`) and in the WS proxy's own docstring ("Loopback-only target; the Tailscale perimeter is the auth boundary (§9)", `browser_routes.py:262-263`).

Making the view writable upgrades "anyone on the tailnet can watch" to "anyone on the tailnet can drive." **This does not change the trust calculus**, because the tailnet can *already* drive: `POST /browser/click|type|key|scroll` are open, unauthenticated endpoints on the same perimeter (`browser_routes.py:108-159`), as are `/chat`, the app proxy, and the MCP surface. RFB input adds no capability an attacker-on-the-tailnet lacks today. Therefore: no app-layer auth added; x11vnc stays `-nopw` but strictly loopback-bound, reachable only through the Orchestrator proxy; D11's no-per-operator-gating stance survives the flip (revisit only if the box ever leaves the single-household trust model — §10-D7).

---

## 7. Per-Surface Work

### 7.1 Portal web

- **New:** `Portal/cu-view/` served client (RFB + gesture layer + cursor + extra-keys + keyboard toggle + session switcher + ended/reconnect states). `GET /cu/view/{sid}` serves it (templating just the session id, or the page reads it from its URL and fetches `/cu/sessions`).
- `cu-live-view.js`: pill stays (it's good); panel drops the "watch-only" label (`index.html:1308-1316`); opens the switcher instead of `sessions[0]`.
- `cu-interact.js`: becomes the **fallback** viewer (native mode + `live_view=false` boxes). Entry points route: task-pill "Live" (`ui-setup.js:648-653, 808-815`) and inline-screenshot click (`chat-send.js:1263`) open the streaming client when the task has a live virtual session (`cu_session` SSE / `/cu/sessions` lookup), else fall back to cu-interact. Fix its resolution-staleness bug regardless: `/browser/status` in virtual mode returns a sessions list, not `cu_resolution` (`browser_routes.py:56-84`), so `cu-interact.js:30-53` silently keeps 1280x720 — wrong for a Gemini session.
- Retire the legacy in-bubble 3s polling panel (`chat-send.js:406-478`) in favor of a thumbnail + "open live" affordance.

### 7.2 Android MVP — recommendation: WebView reuse, not native

**Promote the dead code.** `CuLiveViewScreen.kt` (complete WebView client, `ui/cu/CuLiveViewScreen.kt:16-36`) + `CuSessionsClient` get a NavGraph route and a sessions badge (mount point context: `NavGraph.kt:194` where CuScreen lives). Because the client is the server-served page, every gesture/cursor/extra-keys feature lands on Android with **zero Kotlin UI code**. Native rebuild (Compose gesture layer + RFB lib) is rejected for v1: 3× the work, breaks one-page parity, and the map shows no evidence WebView touch is inadequate — validate on the Fold first.

Android-specific work: nav route + badge; WebView settings check (multi-touch pinch does work in WebView but must be validated with the overlay's `touch-action: none`; keep JS on, file access off as already done); back-press and IME interplay with the manual keyboard toggle; **CuScreen.kt stays as the fallback** (native mode / no-websockify) with the §4.4 auto-keyboard removal applied.

### 7.3 WebView wrapper

No dedicated work: `PortalActivity.kt` (`:76, :487`) renders the Portal verbatim and inherits the touch-first client. Verification only: multi-touch pinch inside the wrapper's WebView, and the RFB WS handshake through the wrapper's origin (wss over Tailscale HTTPS).

---

## 8. Milestones

Sized for the workflow: **M0-M4 are dev-box buildable/testable** (after M0 installs the two packages); **M5-M6 include MS02/Fold live validation**. Each milestone leaves the tree shippable (prod runs live from the working tree — `feedback_toolvault_v2_migration`).

- **M0 — Provisioning + prerequisites (dev-box).**
  `sudo apt install websockify novnc` on the dev box (both already SHOULD_HAVE in `Scripts/onboarding/system-packages.txt:26-27`; MS02 done) → `live_view=True` locally; confirm the existing watch-only path end-to-end. Decide-and-do §10-D4: fix the OpenAI executor binding (`openai_cu/agent_loop.py:163` uses a fresh default `ActionExecutor()` instead of the session-rebound one from `session_manager.py:214-219`) and assess Gemini (fresh default executor `gemini_cu/agent_loop.py:210` + **no per-session virtual display at all**, deferred per `headless.py:~455-461`). If Gemini's display work is deferred again, v1 panel scopes to sessions that exist in `/cu/sessions` (anthropic + openai) — honest, additive.
- **M1 — Interactive viewer v1 (dev-box).**
  Promote `_CU_VIEW_HTML` to the served `Portal/cu-view/` asset; `viewOnly` off; direct mouse input working (desktop web); ended-session card + bounded reconnect; session switcher; noVNC sourcing decision executed (§10-D3).
- **M2 — Touch layer (dev-box + phone browser).**
  Overlay interception; touchpad cursor (drag/tap/double-tap/tap-drag/two-finger right-click/two-finger scroll); pinch-zoom + pan with `scaleViewport` off; edge-push panning.
- **M3 — Keys (dev-box + phone browser).**
  Extra-keys bar (rows, sticky modifiers, selectable set) over RFB keysyms; manual keyboard toggle; kill auto-open in the fallbacks (`CuScreen.kt:~700, ~1495-1500`; `cu-interact.js:132`).
- **M4 — Portal integration (dev-box).**
  Entry-point routing (pill "Live", inline screenshot, drawer) → streaming client with cu-interact fallback; `cu-interact.js` resolution fix; retire the in-bubble poller; narration/E-stop top bar.
- **M5 — Android (Fold-validated).**
  NavGraph route + badge for `CuLiveViewScreen`/`CuSessionsClient`; WebView gesture validation on the Fold; CuScreen fallback keyboard fix; wrapper verification pass.
- **M6 — Live validation + hardening (MS02).**
  Full pass on MS02 with 3 concurrent sessions (switcher, multi-viewer, TTL-reap-while-watching, Orchestrator-restart orphan reconnect); latency/bandwidth sanity over Tailscale (not just LAN); docs + snapshot.

### Additive-preserve checklist (every milestone)

- [ ] CU agent drivers, resolutions (`display.py:33-40`), tools, and injection semantics unchanged (M0's binding fix excepted, explicitly scoped as a bug fix).
- [ ] `/browser/screenshot/live` + `/browser/*` REST endpoints and `cu-interact.js`/`CuScreen.kt` **kept** as the degraded fallback (native mode, `live_view=false` boxes) — preflight already distinguishes the states (`preflight.py:~126-132`).
- [ ] `/cu/sessions`, `/cu/view/{sid}`, `/cu/view/{sid}/ws` contract changes additive-only (Android's parsed client + the pill depend on them).
- [ ] Fresh-box path verified (`feedback_production_quality_portable`): no hardcoded hosts/operators; `live_view=false` degrades to the fallback viewer, never a dead panel.
- [ ] Three surfaces ship together (`feedback_frontend_three_surfaces`) — satisfied structurally by the shared served page + fallback fixes.
- [ ] Never `git add -A`; embeddings.json never re-committed.

---

## 9. Contracts Touched (delta summary)

| Contract | Change |
|---|---|
| `GET /cu/view/{session_id}` | Serves the new asset-based interactive client (was inline watch-only HTML). Same URL — Android WebView + Portal iframe pick it up with zero consumer change. |
| `WS /cu/view/{session_id}/ws` | Unchanged (already bidirectional; client-to-upstream pump exists, `browser_routes.py:286-297`). |
| `GET /cu/sessions` | Unchanged shape; now also feeds the switcher + client sizing. |
| `/browser/click\|type\|key\|scroll`, `/browser/screenshot/live` | Unchanged — fallback path only. (No `session_id` param added; RFB is the session-scoped input channel.) |
| Chat SSE `cu_*`, `/tasks` narration | Unchanged; consumed additively by the panel top bar. |

---

## 10. Open Decisions (with recommendations)

- **D1 — Flip D11 (viewOnly → interactive).** *Recommend: yes.* The one-line client-side gate (`browser_routes.py:237`) is the only thing between us and session-correct input; the perimeter argument (§6) shows no new exposure. This formally closes local-model-stack Q5 as "takeover, etiquette-guarded."
- **D2 — Input channel: RFB vs extended REST.** *Recommend: RFB* (pointer + keysym events through the existing proxy). Session-correct by construction, expresses hover/drag/modifiers/F-keys that the REST allowlist can't, zero backend change. Cost: bypasses the `interaction.py` audit seam — acceptable since that seam captures nothing today beyond the allowlist, and the REST path survives as the native-mode fallback. Do **not** build parallel session-scoped `/cu/{sid}/input` REST endpoints in v1.
- **D3 — noVNC sourcing: apt 1.3.0 mount vs vendor 1.5.x into `Portal/vendor/novnc`.** *Recommend: vendor 1.5.x* and point the `/cu/novnc` mount at the repo copy (keep the `/usr/share/novnc` mount as fallback). Rationale: fresh-box robustness (apt novnc is SHOULD_HAVE and demonstrably missing on this very box) and materially better touch/gesture APIs in 1.5.x. Yes, it's a new pattern (Portal's libs are CDN — `index.html:13-20`) — but a pinned vendored copy is the *more* production-portable pattern, and CDN is unavailable offline anyway.
- **D4 — Prerequisite scope: OpenAI/Gemini executor display-binding + Gemini's missing per-session display.** *Recommend:* fix the OpenAI one-liner in M0 (`openai_cu/agent_loop.py:163` → use `session.actions`, matching Anthropic via `session_manager.py:214-219`); for Gemini, keep the deferral (`headless.py:~455-461`) and scope the v1 panel to sessions listed in `/cu/sessions` — the panel never *claims* to show a display the agent isn't on. Gemini per-session displays become their own follow-up.
- **D5 — Arbitration depth.** *Recommend:* v1 = etiquette + client-side "agent acting" dimming (§5); no server mutex. Add a server-side pause-on-user-input only if dogfooding shows real fights.
- **D6 — Native-mode streaming.** *Recommend:* out of scope; screenshot-poll fallback covers it correctly. Future: on-demand x11vnc against the physical X server behind the display arbiter.
- **D7 — Auth posture for writable view.** *Recommend:* keep Tailscale-perimeter-only (§6). Log a `[CU-VIEW]` line on WS connect (already partially there for failures) for visibility; no per-operator gating.
- **D8 — Fate of the polling path.** *Recommend:* keep permanently as the degraded fallback (native mode + no-websockify boxes), not deleted — the fresh-box gate demands a working panel with zero SHOULD_HAVE packages.
- **D9 — Android native client.** *Recommend:* WebView reuse (§7.2); revisit native only on measured Fold gesture-latency failure.

---

## 11. Risks

- **WebView gesture fidelity** (pinch inside Android WebView with `touch-action: none` overlays; IME interplay) — mitigated by M5 Fold validation before calling it shipped; native client is the escape hatch (D9).
- **apt noVNC 1.3.0 API drift** if vendoring (D3) is rejected — 1.3.0's RFB module lacks some 1.5.x affordances; gesture layer must then avoid private APIs.
- **Xvfb has no hardware cursor** — x11vnc sends the client the X cursor; with an indirect pointer the *virtual* cursor is the truth and the remote cursor rendering should be suppressed client-side (noVNC renders server cursor via cursor pseudo-encoding; disable/ignore to avoid double cursors).
- **Orchestrator restart** orphans quartets until boot-reap; viewer must treat WS drop + session-still-listed as "reconnect," not "ended."
- **Latency over Tailscale WAN** (DERP-relayed clients) may make the touchpad feel laggy — the stream stays usable (VNC degrades gracefully); document expectation, no speculative WebRTC work (`feedback_prove_with_data`).
