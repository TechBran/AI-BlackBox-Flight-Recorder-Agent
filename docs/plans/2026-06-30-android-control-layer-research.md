# Android / Tablet / XR Device-Control Layer — Research

**Date:** 2026-06-30 · **Status:** RESEARCH (pre-brainstorm, pre-plan) · **Author:** Claude (Opus 4.8) for Brandon

Goal driving the research: **decouple device control from the on-device Gemma 4 model** (too slow — ~75s cold load) and let **frontier models (Claude/Gemini/OpenAI) drive the device end-to-end** via the intent layer + a screen-vision loop, across **phones, tablets, and XR goggles**. On-device Gemma stays only when the user explicitly picks it as the provider.

Produced by a 12-agent research workflow (6 internal codebase readers, 6 external domain researchers). Citations are `file:line` (internal) or URLs (external). Everything below is **findings, not a plan.**

---

## 0. Headline

**This is a rewire, not a rebuild.** The Android app already ships a complete, provider-agnostic actuation layer that a cloud model can drive:

| Capability | Where | Status |
|---|---|---|
| Tap / click | `overlay/Actuators.kt:98` — prefers `AccessibilityNodeInfo.ACTION_CLICK` on resolved node/ancestor, falls back to coordinate `dispatchGesture` | real (565 LOC) |
| Type | `Actuators.kt:194` — `ACTION_SET_TEXT`, with credential-handoff gate | real |
| Swipe / scroll | `Actuators.kt:265,386,404` — `dispatchGesture` | real |
| Back / home / recents | `Actuators.kt:372` — `performGlobalAction` | real |
| Read screen (a11y tree) | `overlay/UiTreeReader.kt:53` — JSON nodes w/ resourceId + bounds(l,t,r,b) + clickable + password-redaction, capped 80 nodes | real (399 LOC) |
| Screenshot | `overlay/ScreenCapture.kt` + `OverlayService.kt:1802` — MediaProjection→ImageReader→PNG, password-field refusal gate, ephemeral | real (122 LOC) |
| Intents (14) | `overlay/IntentActuator.kt:103` — dial/sms/email/maps/url/settings/alarm/timer/calendar/contact/photo/wifi/flashlight/share, Application-Context, **no a11y needed** | real (387 LOC) |
| Dispatch seam | `overlay/AndroidPhoneController.kt:66` — `PHONE_ACTUATORS` + forwards `INTENT_ACTIONS` | real (210 LOC) |
| Planner (today) | `data/local/FcLoop.kt` — the ONLY Gemma-specific piece; emits the tool calls | swap point |

The redesign = **move the ReAct planner "brain" from on-device Gemma (FcLoop) to a cloud frontier model**, keep the actuators as the "hands." The a11y config declares `canRetrieveWindowContent` + `canPerformGestures` (not `canTakeScreenshot`).

---

## 1. Current architecture — the two existing device-control paths

**Path 1 — `control_phone` (the slow one to replace):** frontier model → ToolVault `control_phone` executor → POST natural-language task to the phone's `RemoteControlServer` (NanoHTTPD, **Tailscale port 8765**, `/task` + `/status` + `/healthz` + `/notify`) → `RemoteTaskRunner` **wakes on-device Gemma** (cold load 10–75s) → Gemma runs the native agent loop, dispatching through `RemoteAllowlist`-gated Actuators/IntentActuator → results returned via `/status` polling (2s interval, 300s total timeout, 60s lost-contact grace).
- Refs: `ToolVault/tools/control_phone/{executor.py,schema.json}`, `data/remote/RemoteControlServer.kt`, `data/remote/RemoteTaskRunner.kt`, `data/local/EngineWarm.kt`.
- **Gemma is the actuator brain here** — the inference loop, tool selection, and agent loop all run on-device. The bottleneck is Gemma cold-load + per-step inference, *not* network.
- Transport already has: per-request operator isolation, Tailscale perimeter auth (`isTailnetSource` CGNAT/ULA/loopback + `boundOperator == requestOperator`), device resolution via `mesh.resolve_origin(operator)` joining `tailscale status` liveness with attestation registry (`tailnet_name` key).

**Path 2 — `control_android_device` (a working frontier prototype):** frontier **Gemini Computer Use** drives an Android device over **ADB** — `Orchestrator/gemini_cu/agent_loop.py:276` (android branch) denormalizes 0-999 coords via `wm size` and injects `input tap/swipe/text/keyevent`, `am start`/`monkey` for launch, `screencap` for perception. Default model `gemini-2.5-computer-use-preview-10-2025` (`config.py:155`). **Requires USB/wireless debugging** — a customer-onboarding/security burden, and does NOT work for XR.

**Model-free reachability:** `NotificationListenerFgs` owns port 8765 permanently (boot-survivable, `connectedDevice` FGS type dodges the 6h `dataSync` timeout; re-armed by `BootReceiver`). `RemoteTaskHandlerHolder` swaps the handler: Gemma's `RemoteTaskRunner` when a model loads, `NoopRemoteTaskHandler` otherwise. LMK reclaims the ~6GB Gemma process when backgrounded; `WarmInflightStore` + `ACTION_START` vs `ACTION_START_LISTENER` prevent a crash-loop.

---

## 2. Domain landscape (external)

### 2a. Actuation mechanism — AccessibilityService is the standard
One user-enabled toggle grants four load-bearing primitives, **none dependent on any model**:
- `dispatchGesture()` (API 24, `canPerformGestures`) — synthetic taps/swipes/long-press/multi-touch, OS-indistinguishable from real touch.
- `AccessibilityNodeInfo` tree (`canRetrieveWindowContent`) — find by resource-id/text, act via `ACTION_CLICK`/`ACTION_SET_TEXT`. **Coordinate-free, more reliable than pixel taps.**
- `performGlobalAction()` — BACK/HOME/RECENTS/NOTIFICATIONS/QUICK_SETTINGS/SCREENSHOT.
- `takeScreenshot()` (API 30, `canTakeScreenshot`) — silent capture, **no MediaProjection prompt/indicator**, rate-limited ~333ms (~3fps).
- `FLAG_SECURE` (banking/DRM) → black screenshot but **the node tree still reads** → vision degrades, element actuation survives.
- Every open-source phone-agent (DroidRun ~91% AndroidWorld, AppAgent, Mobile-Agent, AutoDroid) uses this recipe.

### 2b. Screen capture — two options, opposite tradeoffs
- **MediaProjection** — continuous 30-60fps, but **per-session consent dialog** + Android 14+ single-use token + `FOREGROUND_SERVICE_MEDIA_PROJECTION` + Android 15 status-bar chip + auto-stop on lock. (App uses this today, single-frame.)
- **`AccessibilityService.takeScreenshot()`** — silent, no per-session prompt, survives backgrounding/lock, ~3fps cap. **Better default for a multi-step frontier loop** (which doesn't need 30fps). Requires adding `canTakeScreenshot` to the a11y config (not currently declared).
- Frontier vision sizing: Claude ≤1568px/1.15MP (≤2576px on Sonnet 5 / Opus 4.8/4.7), rescale coords back yourself; dense text wants 2000-2500px.

### 2c. Intent layer — the fast, deterministic path
- Fixed catalog of "common intents" (dial/sms/email/maps/nav/alarm/timer/calendar/settings-panels/web/deep-links) — instant, no vision, but **bounded** (only actions an app declared an intent-filter for). Cannot generically "tap button X."
- `Android 11+` package visibility (`<queries>`/`QUERY_ALL_PACKAGES`) restricts resolving arbitrary apps → need a curated app/deep-link registry.
- **Do NOT build on Google Assistant App Actions/BIIs — Assistant is retired March 2026.** Fire intents/deep links directly.
- **Android 17 AppFunctions** (private preview 2026) makes apps behave like on-device MCP servers Gemini can invoke — the forward-looking OS-native tool surface.

### 2d. Frontier computer-use — provider reality
| Provider | Android env? | Coordinates | Notes |
|---|---|---|---|
| **Gemini 3.5 Flash** | **YES — native `environment:'mobile'`, 11 Android actions** (open_app, click, type, long_press, drag, press_key, go_back, wait, list_apps, take_screenshot, scroll) | normalized 0-999 | Only vendor-VALIDATED Android path; official `gemini-android-computer-use-quickstart` (ADB, emulator-only, "not an official product") |
| Gemini 2.5 CU | browser-first | 0-999 | what `control_android_device` uses today |
| **Anthropic** computer use | **NO (desktop-only)** | absolute px, ≤1568/2576px | DIY on Android via generic pixel action space + our a11y bridge |
| **OpenAI** `computer` (gpt-5.4/5.5) | **NO (browser/mac/win/ubuntu)** | absolute px | same DIY caveat |

**Perception is hybrid in SOTA:** feed screenshot **+** a11y tree (or Set-of-Mark numbered overlay); prefer element-precise `ACTION_CLICK` on stable resource-ids, fall back to coordinate taps for tree-blind surfaces (Compose/WebView/games). Our `Actuators` already prefers this order. `config.py:165` already regex-detects anthropic/google/openai CU backends → multi-provider selection infra exists.

---

## 3. Form-factor matrix

| Surface | Intent path | Node/a11y actuation | Coordinate gesture | Screen capture | Verdict |
|---|---|---|---|---|---|
| **Phone** | ✅ | ✅ | ✅ | ✅ MediaProjection / takeScreenshot | primary target, all paths work |
| **Tablet / foldable** | ✅ | ✅ | ⚠️ fix `Actuators.swipe/scroll` (uses deprecated `resources.displayMetrics` → wrong on large-screen/multi-window; move to WindowMetrics). Multi-window coords are display-relative — model needs window topology. Fold/rotate/resize invalidate coords. | ✅ (size from WindowMetrics; Android 14 `onCapturedContentResize`) | works with display-addressing fixes |
| **DeX / desktop windowing** (Android 16 default) | ✅ (needs launch-display-id) | ✅ | ✅ `GestureDescription.Builder.setDisplayId()`; external DeX = separate displayId | `takeScreenshot(displayId)` | needs display-addressed capture+gesture |
| **XR headset (Galaxy XR / Android XR)** | ✅ **ports ~unchanged** (2D apps = spatial panels; full Android) | ✅ *should* transfer (TalkBack ships; node ACTION_CLICK) — **unverified on device** | ❌ **does NOT map cleanly** (per-panel 3D compositor, no flat framebuffer; input = gaze/pinch) | ❓ **UNCONFIRMED** — headset-view capture not documented on Android XR (confirmed on Quest); passthrough is compositor-protected/un-capturable | node + intent actuation only; vision loop is the open risk |
| **Glasses (audio/display)** | ✅ on paired phone | via phone | via phone | via phone | **compute lives on the paired PHONE (Jetpack Projected) → "drive the phone, glasses are I/O"**, not on-glasses actuation |
| **TV** | ✅ | D-pad focus, not gesture | ❌ | — | different model; likely out of scope |

**XR bottom line (the emphasized ask):** Android XR is Android underneath, so **intents + a11y-node actuation are the portable substrate**; coordinate-vision does NOT port (no flat screen, capture unconfirmed). Gemini is the vendor-native XR brain. Near-term XR control = frontier planner + a11y-tree/intent actuation, NOT a pixel-coordinate loop. Treat "see the headset view + click" as unproven pending real hardware.

---

## 4. Constraints & risks

- **Google Play policy (published 2025-10-30, enforced 2026-01-28)** explicitly prohibits AccessibilityService use by apps that "autonomously initiate, plan, and execute actions" — i.e. exactly LLM screen-agents. The app **cannot** claim `isAccessibilityTool` (assistants/automation are named ineligible). → **Ship sideloaded / direct-APK / enterprise (BlackBox already is), never via Play.**
- **Android Advanced Protection Mode** (Android 16 canary → ~17 stable, opt-in) can **OS-revoke** the a11y permission for non-`isAccessibilityTool` apps. → design for **graceful loss of a11y** (fall back to the intent path).
- **Coordinate contract differs per provider** (abs-px vs 0-999) → need a per-backend coordinate adapter at the actuation boundary (repo already denormalizes 0-999 for Gemini).
- **Latency** shifts from Gemma cold-load (gone) to per-step screenshot round-trip (the universal CU cost) → mitigate with a11y-tree-only steps when the tree is rich, batch actions/turn, streaming (SSE/WebSocket) over 2s polling, adaptive multi-step timeouts.
- **Safety must extend to the cloud path:** password-field redaction (`UiTreeReader`/`ScreenCapture`), credential handoff (user types secrets, model never sees them), high-consequence confirm-gates (send/pay/delete keywords; send_sms/send_email), autonomy PERMISSION/YOLO. Streaming screen state to a cloud model IS the data-exfil pattern Google restricted → keep redaction + ephemerality + an explicit "AI is viewing your screen" consent surface. The current `RemoteAllowlist` is deliberately conservative (14 safe actions, refuses send_sms/dial) — "everything available" means EXPANDING it, which shifts the safety boundary onto the confirm-gate/consent model.

---

## 5. Strategic options surfaced (for brainstorm — NOT decided)

- **Option A — server-side frontier CU + ADB** (extend `control_android_device`): low-effort, switch default to Gemini 3.5 Flash `environment:'mobile'` (only vendor-validated Android path). ✅ fast to ship. ❌ ADB onboarding/security burden; no XR; Gemini-only for the validated path.
- **Option B — rewire the Tailscale channel; phone = thin hands, cloud = brain** (the strategic target): phone streams observation (a11y tree + optional screenshot) UP over 8765; cloud frontier ReAct loop emits actions; server relays to the SAME `Actuators`/`IntentActuator`. ✅ no ADB, works on any consented device incl. XR, reuses the whole actuation layer, provider-agnostic. ❌ more to build (server-side agent loop, streaming, per-provider coordinate adapter, allowlist expansion, consent UX).
- **Hybrid** — B as the architecture; use Gemini-CU-mobile as the first validated planner; keep intents as first-class fast-path tools; keep Gemma as an explicit opt-in provider behind the same seam.

## 5.5 Confirmed brainstorm decisions (2026-06-30)

1. **Architecture = Hybrid, Path B seam, Gemini-mobile first.** Phone is a provider-agnostic "thin hands" endpoint over the existing Tailscale 8765 channel; cloud frontier model is the brain; first validated planner is Gemini 3.5 Flash mobile CU; intents stay a fast deterministic path; on-device Gemma becomes an explicit opt-in provider behind the same seam. Provider-agnostic (Claude/OpenAI via per-backend coordinate adapter) comes later.
2. **Grounding = Full hybrid (tree + vision).** Each step, feed the model BOTH a screenshot AND the a11y-tree (element ids/labels/bounds, Set-of-Mark style). Model may target an element (`ACTION_CLICK` on a stable resource-id — reliable) OR a coordinate (`dispatchGesture` fallback for tree-blind Compose/WebView/games). Observation (tree + screenshot) is captured on the TARGET device. Providers that can't take injected context degrade to vision-coords + server-side element snapping.
3. **Device routing (FIRM requirement, Brandon):** the control target **DEFAULTS to the device the request originated from** — "the call comes back to the device the call came from." An explicit target may name **any device on the tailnet** to run any task. Invariant: **never silently route to a device other than the originator** unless explicitly told. Implementation: the request must carry the originating device's tailnet identity; the resolver defaults the target to that origin device (not just `mesh.resolve_origin(operator)`), with an optional explicit `device` param spanning the full tailnet. Resolved: when the request does NOT originate from a tailnet device (Portal on the box, or a remote MCP client), the target **defaults to the operator's primary/attested device** (designated once, or auto = only/most-recent device); the explicit `device` param still overrides. So: origin-device → that device; non-device origin → operator's primary device; explicit → any tailnet node; never a silent retarget.
4. **Safety posture = full suite, keep the smart gates.** The conservative `RemoteAllowlist` is removed — the frontier model can reach ALL actuators + intents (including today's refused `send_sms`/`dial`). Preserved: the high-consequence confirm-gate (send/pay/delete/post surface a confirm on the target device before firing), credential-handoff (passwords/payment fields → the USER types, the model never sees or enters them), and password-field screenshot redaction. Autonomy (YOLO vs confirm-each) is user-selectable per device/session. Safety boundary moves from a static allowlist to the confirm-gate + credential-handoff + consent surface.
5. **XR = co-equal from day 1** (phone + tablet + Galaxy XR headset together). XR actuation path is **node + intent only** (no coordinate-vision — the per-panel 3D compositor has no flat framebuffer). The XR observation loop must work **without screenshots** (a11y-tree + intents), because headset-view capture is UNCONFIRMED on Android XR. Two hardware-dependent unknowns now gate v1 and must be de-risked EARLY on a real Galaxy XR device: (a) does `dispatchGesture`/`AccessibilityNodeInfo.ACTION_CLICK` actuate a 2D app panel inside the XR compositor? (b) can a third-party service capture the composited panel view at all? Glasses remain "drive the paired phone" (Jetpack Projected), reusing the phone stack. **Requires Galaxy XR hardware.**
6. **XR build cadence = in parallel, validated continuously** (Brandon has the Galaxy XR hardware). No separate de-risk gate. Risk mitigation baked into the contract: the observation channel exposes a **per-device capability flag** (`hasVision`/`hasScreenshot`, `supportsCoordinateGesture`) so a device advertises what it can do; the loop uses tree+intent when vision/coordinate aren't available. This makes an XR capture-or-panel-actuation surprise a graceful degradation (tree+intent still works) rather than a contract rework. Validate the two XR unknowns on-device early even without a formal spike milestone.
7. **Capture/consent = silent a11y screenshot + visible in-app consent.** Use `AccessibilityService.takeScreenshot()` (silent, ~3fps, survives backgrounding/lock, no per-session system prompt) for the vision loop, ALWAYS paired with a persistent in-app "AI is controlling this device" banner + instant kill switch. Add `canTakeScreenshot` to `accessibility_service_config.xml` (currently only `canRetrieveWindowContent` + `canPerformGestures`). Preserve password-field redaction on this path. Tree-first cadence is a recommended optimization (screenshot when the tree is blind or the model requests it) to cut cloud data + latency.
8. **Device/provider management = managed-state onboarding flow** (Brandon). Per-operator devices are configured in the managed-state onboarding/hub console (reuse the onboarding-uplift `?mode=manage` hub, not a new surface): the operator designates a **primary device** (the source of truth for the "non-device origin → operator's primary device" routing fallback), sets a **per-device default provider** (Gemma is an explicit opt-in choice here), and can **add/edit devices** for their operator later. Accepted with this: **streaming transport** (SSE/WebSocket over 8765, replacing 2s polling) and **coordinate-adapter sequencing** (Gemini 0-999 for v1; Anthropic/OpenAI absolute-px adapter when those providers join).
9. **Intent layer = comprehensive / full phone capability** (FIRM requirement, Brandon). The Android-side intent layer (`overlay/IntentActuator.kt`, today 14 intents) is EXPANDED to handle ~ALL intent processing, so the frontier model reaches the phone's FULL capability via the fast, deterministic intent path — reserving the slower vision/accessibility loop only for in-app "tap button X" that no intent can express. Scope = the complete Android **common-intents catalog** (alarms/timers, calendar, camera, contacts, email, file pickers, maps + turn-by-turn navigation, media play-from-search, phone dial/call, SMS/MMS, web view, sharing) + **`open_app`** (launch any installed package) + **`open_url`** (ACTION_VIEW on any http/https or app deep-link URI — one primitive covering all deep links) + **`open_settings`** (any `Settings.ACTION_*` panel — the full catalog: WiFi/Bluetooth/Location/Display/Sound/Airplane/Data/Apps/Security/Accessibility/Battery/Storage/Date/Language/NFC/Hotspot/…) + a **guarded generic `send_intent(action, uri?, extras?, mime?)`** escape-hatch for the long tail. Handle Android 11+ **package visibility** (`<queries>`/`QUERY_ALL_PACKAGES`) so any installed app is resolvable/launchable. Each intent is a TYPED tool the frontier model calls directly; the decision-4 safety gates (high-consequence confirm, credential-handoff) apply to intents too. Note: the on-device model's `web_search` stays HEADLESS (prior fix, [[project_on_device_tool_restraint_headless_search]]); the FRONTIER device-control path MAY open the browser as a legitimate user-directed action — different context, not a conflict.

## 7. Target design (synthesized from brainstorm)

**Shape:** the phone/tablet/XR device is a provider-agnostic **"hands" endpoint**; a cloud **frontier ReAct loop is the "brain"**; they talk over the existing Tailscale 8765 channel. Gemma out of the actuation loop (opt-in provider only).

**Phone-side (reuse existing, add channels):**
- Observation channel UP: `UiTreeReader` a11y-tree JSON (element ids/labels/bounds, password-redacted) + a **device capability descriptor** (`hasScreenshot`, `supportsCoordinateGesture`, form-factor) + an **optional silent screenshot** (`AccessibilityService.takeScreenshot()`).
- Action channel DOWN: element action (`ACTION_CLICK`/`ACTION_SET_TEXT` by resource-id/node) · coordinate gesture (`dispatchGesture`, fallback) · global action (back/home/recents) · intent (the 14 deterministic OS intents). All through the existing `Actuators`/`IntentActuator`.
- Reuse: `Actuators`, `UiTreeReader`, `ScreenCapture`, `IntentActuator`, `AndroidPhoneController`, `RemoteControlServer` (8765), `RemoteTaskHandlerHolder` (make provider-agnostic).

**Cloud-side (new):** a server-side frontier agent loop (first: Gemini 3.5 Flash `environment:'mobile'`) that consumes tree+screenshot, emits element/coordinate/intent actions, and relays them to the target device. Provider-agnostic seam with a **per-backend coordinate adapter** (Gemini 0-999 · Anthropic/OpenAI absolute-px w/ downscale+rescale) added when Claude/OpenAI join. Reuses the existing multi-provider CU backend detection (`config.py:165`).

**The loop:** observe (tree, +screenshot when tree-blind/requested) → frontier decides (prefer element, else coordinate, else intent) → relay to actuators → re-observe. **Streaming (SSE/WebSocket) over 8765**, not 2s polling; **action batching** per turn; **adaptive/long timeout** for multi-step autonomy (Gemma cold-load gone).

**Routing:** request carries the originating device's tailnet id → target defaults to it; non-device origin (Portal/MCP) → operator's primary/attested device; explicit `device` → any tailnet node; never a silent retarget.

**Safety:** full suite; high-consequence (send/pay/delete/post) confirm on the target device; credential-handoff (user types secrets); password-field redaction; per-session consent banner + kill switch; autonomy YOLO/confirm-each per device.

**Grounding:** full hybrid (tree + screenshot); element-preferred `ACTION_CLICK`, coordinate fallback for tree-blind surfaces.

**XR:** co-equal; node+intent path (capability flags gate coordinate/vision); glasses = drive the paired phone.

**Distribution:** sideload/enterprise only (Play prohibits autonomous a11y agents); graceful fallback to the intent path if a11y is OS-revoked (Advanced Protection).

**New vs reused:** NEW = cloud frontier agent loop + observation/action channels on the listener + per-provider coordinate adapter + consent/banner/kill-switch UI + origin-aware routing & device selection + `canTakeScreenshot` config + tablet display-addressing fixes (WindowMetrics, `setDisplayId`). REUSED = the entire actuation layer + transport + auth/operator-isolation.

## 6. Open questions to resolve in brainstorm
1. **Primary architecture:** A (ADB, ship fast) vs B (Tailscale rewire, strategic) vs hybrid? 
2. **Perception default:** a11y-tree-first (reliable, cheap) vs screenshot-first (vision) vs always-both? 
3. **Which frontier provider(s) first** — Gemini (only validated Android CU) vs provider-agnostic from day one? 
4. **Safety posture for "everything available"** — expand the allowlist how far; per-action confirm vs YOLO-with-consent; how the confirm-gate reaches a remote cloud loop.
5. **XR scope now vs later** — ship phone/tablet first with an XR-ready seam, or design XR-first constraints (node/intent-only, no coordinate) in from the start?
6. **Screenshot path** — add `canTakeScreenshot` (silent, ~3fps, no prompt) vs keep MediaProjection (visible consent, high-fps)? 
7. **Distribution/compliance** — confirm sideload/enterprise-only; plan for a11y-revocation fallback.

## Appendix — key source files
Internal: `ToolVault/tools/{control_phone,control_android_device,use_computer}/`, `Orchestrator/gemini_cu/agent_loop.py`, `Orchestrator/adb/commands.py`, `Orchestrator/browser/{driver_anthropic.py,actions.py}`, `Orchestrator/config.py:151-176`, Android `overlay/{Actuators,UiTreeReader,ScreenCapture,IntentActuator,AndroidPhoneController,BlackBoxA11yService}.kt`, `data/local/FcLoop.kt`, `data/remote/{RemoteControlServer,RemoteTaskRunner,RemoteAllowlist}.kt`, `res/xml/accessibility_service_config.xml`, `docs/plans/2026-06-18-frontier-to-phone-control-design.md`.
External: developer.android.com (accessibility, media-projection, XR, intents-common, package-visibility, appfunctions), Play policy `support.google.com/googleplay/android-developer/answer/10964491`, Gemini/Anthropic/OpenAI computer-use docs, DroidRun/AppAgent/AutoDroid/Mobile-Agent, Samsung Galaxy XR.
