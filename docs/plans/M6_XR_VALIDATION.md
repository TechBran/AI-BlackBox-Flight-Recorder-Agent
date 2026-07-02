# M6 — Galaxy XR / Android XR On-Device Validation Checklist

**Date:** 2026-07-02 · **Milestone:** M6 (XR co-equal, node+intent, capture-independent) · **Status:** ON-DEVICE CHECKLIST — run on a real Samsung Galaxy XR (Android XR)

**Plan:** `docs/plans/2026-06-30-android-control-frontier-driven-plan.md` (M6, table 6.1–6.4) · **Research/decisions:** `docs/plans/2026-06-30-android-control-layer-research.md` §3 (form-factor matrix), §5.5 decisions 5/6.

---

## Why this doc exists

M6 was built, unit-tested, and compiled WITHOUT Galaxy XR hardware. The design is deliberately **fail-safe by capability flag**: the device advertises `formFactor=xr_headset`, `supportsCoordinateGesture=false`, `hasScreenshot=false`, and every coordinate/screenshot path degrades to **node (`ACTION_CLICK`) + intent** automatically. So the app will not misbehave on XR even if the two hardware unknowns below resolve unfavorably — the worst case is "an action was skipped and the model re-planned," never a crash or a wrong gesture.

Two hardware-dependent unknowns still **gate M6 closure** and can ONLY be answered on a real headset (research §3, decision 5):

- **Unknown (a) — Actuation:** Does `AccessibilityNodeInfo.ACTION_CLICK` (and `dispatchGesture`) actually actuate a **2D app panel** rendered inside the XR spatial compositor? (a11y-node click is EXPECTED to work; coordinate `dispatchGesture` is EXPECTED to be a no-op / unsupported — verify both.)
- **Unknown (b) — Capture:** Can a third-party `AccessibilityService.takeScreenshot()` / MediaProjection capture the **composited panel view** at all on Android XR? (Unconfirmed; Quest passthrough is compositor-protected. The result SETS whether `hasScreenshot` can ever be true on XR.)

Record each finding in the **Findings log** at the bottom. When steps 1–7 are complete and (a)/(b) are recorded, M6 can be marked validated.

---

## Contract under test (what the build already guarantees)

| Guarantee | Where enforced | Unit test |
|---|---|---|
| XR reports `formFactor=xr_headset`, `supportsCoordinateGesture=false`, `hasScreenshot=false` | `overlay/DeviceCapabilities.detect` (XR probe = `android.software.xr.api.spatial` / `UI_MODE_TYPE_VR_HEADSET`) | `DeviceCapabilitiesTest` |
| Frontier `/action` coordinate_tap/swipe SKIPPED + reported on XR (element/intent pass) | `data/remote/PhoneActionDispatcher` (primary) + `overlay/AndroidPhoneController` (defense-in-depth, M6.1) | `RemoteActionChannelTest`, `AndroidPhoneControllerActionTest` |
| Grounding is element/intent-only on XR (no coordinate fallback) | `Orchestrator/frontier_grounding.snap_to_element` / `snap_swipe_to_coordinate` (`supports_coordinate=False`) | `test_frontier_grounding.py` |
| Loop never captures/requests a screenshot when `hasScreenshot=false` | `overlay/ObservationBuilder` (`shouldCaptureScreenshot`) + `frontier_agent_loop` (no screenshot part; prompt says "Do not request a screenshot"; coordinate tools pruned) | **Load-bearing:** `ObservationTest` (device omits `screenshot` from the wire) + `test_mobile_system_prompt_forbids_screenshot_on_capture_less_device` (prompt forbids it) + `test_build_mobile_tools_prunes_coordinate_functions_on_xr` (coordinate tools pruned). *Corroborating (wire/routing only):* `test_xr_loop_drives_element_intent_only_with_zero_screenshots` — its FakeDriver bypasses the real screenshot path, so it proves observations carry no screenshot + coordinate ops are fed-back-never-posted, NOT that the loop refrains from requesting one. |
| In-headset "AI is controlling this device" banner + STOP (M6.3); notification STOP is the headless fail-safe | `overlay/XrExpandedPanel` (STOP lives in the **expanded** panel) + `XrBubbleContent` (collapsed bubble shows a red **CTRL** chip → expand to reach STOP) (bridge `controlSessionActive`) + `NotificationListenerFgs` (always-on notification STOP = fail-safe) | `OverlayBridgeTest` |

**Glasses note:** display/audio glasses are OUT of scope for on-headset actuation — they **drive the paired phone** (Jetpack Projected), reusing the entire phone stack. This checklist is for the standalone Galaxy XR headset only.

---

## Constants used below

```
PKG=com.aiblackbox.portal
A11Y=com.aiblackbox.portal/com.aiblackbox.portal.overlay.BlackBoxA11yService
PORT=8765                       # RemoteControlServer (NotificationListenerFgs owns it)
OP=<your operator>              # must equal the device's bound operator (BlackBox settings)
XR=<headset tailnet dns or ip>  # e.g. galaxy-xr.tailXXXX.ts.net  (tailscale status)
```

The 8765 channel is **tailnet-gated** (`isTailnetSource`) and **operator-scoped**. Run the `curl`s either **from another tailnet node** using `$XR`, or **on the headset itself over loopback** via adb (loopback is allowed):
```bash
# loopback-on-device variant of any curl below:
adb shell "curl -s http://localhost:$PORT/healthz"
```

---

## Step 0 — Prerequisites

- [ ] Galaxy XR in developer mode; `adb devices` lists it (USB or `adb connect` over Wi-Fi/tailnet).
- [ ] Headset joined to the **same tailnet** as the box (`tailscale status` shows it). Note `$XR`.
- [ ] The operator you'll use is **bound on the headset** (BlackBox app → settings) and matches `$OP`. A blank/mismatched operator fail-closes every 8765 route (by design).

**Expected:** all three true. **Record:** `$XR`, `$OP`.

---

## Step 1 — Sideload + enable the accessibility service

```bash
adb install -r app-debug.apk          # or the signed release APK
adb shell settings put secure enabled_accessibility_services "$A11Y"
adb shell settings put secure accessibility_enabled 1
# verify it stuck:
adb shell settings get secure enabled_accessibility_services
```

> **Caveat (shared device):** `settings put ... enabled_accessibility_services "$A11Y"` **OVERWRITES** the whole enabled-services list — it will DISABLE any other accessibility service already on. If the headset has others you need to keep, APPEND instead (colon-separated): read the current value, then write `"$existing:$A11Y"` (skip if `$existing` already contains `$A11Y`). On a dedicated test headset the overwrite is fine.
Also open the BlackBox app once on the headset so `NotificationListenerFgs` binds the 8765 socket (foreground service), and grant notification permission (for the fail-safe STOP).

**Expected:** the settings query returns a string containing `BlackBoxA11yService`; the app shows its overlay orb in-headset.
**How to record:** paste the `settings get` output. **Contract meaning:** the a11y grant is the load-bearing consent for node actuation; without it every actuator returns `accessibility service not enabled` (graceful, but nothing actuates).

---

## Step 2 — Confirm the XR capability contract

Fetch one observation (SSE — one `data:` frame, then the stream closes):
```bash
curl -sN "http://$XR:$PORT/stream/probe1?operator=$OP" | sed -n 's/^data: //p' | head -1 | python3 -m json.tool
```
Also check health (model-free, no operator needed):
```bash
curl -s "http://$XR:$PORT/healthz"      # -> {"ok":true|false}
```

**Expected:** the observation's `device_capability` is:
```json
{"formFactor":"xr_headset","hasScreenshot":false,"supportsCoordinateGesture":false,"displayId":0}
```
and `ui_tree` is a non-empty array of a11y nodes with **NO `screenshot` field anywhere** in the frame.

**Also confirm the app is in XR-UI mode** (not just the wire flag): start the overlay and verify the app shows its **spatial XR overlay** — the floating orb, and the expanded panel on tap — NOT the flat phone bubble/expanded-panel. Both the wire `formFactor` AND the overlay UI are now driven by the SAME probe (`DeviceCapabilities.isXr`, I1), so if the wire says `xr_headset` but you see the phone overlay (or vice-versa), that divergence is a bug — record it. This is the on-device check that `isXrDevice` (overlay routing) agrees with `detect()` (wire capability).

**How to record:** paste the `device_capability` object + note whether a `screenshot` key was present (it must be ABSENT) + note which overlay UI rendered (XR spatial orb/panel vs. phone bubble).
**Contract meaning:** confirms `DeviceCapabilities.detect` classifies the headset correctly AND that the overlay surface matches. If `formFactor` is NOT `xr_headset`, the XR probe feature/UiMode differs on this headset → capture the actual `formFactor` and the output of `adb shell pm list features | grep -i xr` so the probe set (`DeviceCapabilities.XR_SYSTEM_FEATURES`) can be extended. (Because routing and the wire share one probe, extending `XR_SYSTEM_FEATURES` fixes BOTH at once.)

---

## Step 3 — Unknown (a): does `ACTION_CLICK` actuate a 2D-panel app node?

Open a simple 2D app in a spatial panel on the headset (e.g. Settings or a note app). Read the tree, pick a clickable node, and drive an **element_click** by its `resource_id` (preferred) or `node_id`:

```bash
# 3a. read the tree, find a clickable node's resource_id / node_id
curl -sN "http://$XR:$PORT/stream/probe2?operator=$OP" | sed -n 's/^data: //p' | head -1 | python3 -m json.tool

# 3b. drive an element_click on the chosen node (replace resource_id)
curl -s -X POST "http://$XR:$PORT/action" -H 'Content-Type: application/json' -d '{
  "msg":"action","task_id":"probe2","operator":"'"$OP"'",
  "type":"element_click","resource_id":"<paste resource_id>"
}' | python3 -m json.tool
```

**Expected:** `action_result` `{"success":true,"detail":"tapped node[...]"}` **and the panel visibly reacts** (the button activates). Confirm the second half visually in the headset.
**How to record:** paste the `action_result`; note YES/NO the panel actually reacted. Try 3–4 different node types (button, list row, toggle, icon-only via `contentDescription`).
**Contract meaning — resolves Unknown (a):** if node click actuates the panel, XR node+intent control is CONFIRMED (the M6 core path works). If it succeeds on the wire but the panel does NOT react, log which node types fail — that scopes the XR a11y limitation and whether intents must cover more.

---

## Step 4 — Unknown (a, cont.): does a coordinate `dispatchGesture` do anything on a panel?

Send a **coordinate_tap** — the contract EXPECTS it to be skipped (coordinate-less device):
```bash
curl -s -X POST "http://$XR:$PORT/action" -H 'Content-Type: application/json' -d '{
  "msg":"action","task_id":"probe2","operator":"'"$OP"'",
  "type":"coordinate_tap","x":500,"y":900
}' | python3 -m json.tool
```

**Expected (contract):** `{"success":false,"error":"invalid_argument","detail":"coordinate gestures not supported on xr_headset"}` — the gesture is **skipped + reported**, never dispatched. This confirms the M6.1 gate.
**How to record:** paste the `action_result`; confirm it matches the expected skip.

**Optional empirical probe (only with a debug build):** to actually answer "would `dispatchGesture` do anything on a panel," temporarily bypass the gate (e.g. a debug build that forces `supportsCoordinateGesture=true`, or call `Actuators.tap(x,y)` from a debug hook) and observe whether the panel reacts. **Do NOT ship this.** Record the finding: if `dispatchGesture` is a confirmed no-op on panels, the coordinate-less contract is empirically justified; if it partially works, note it as a future enhancement (still not needed for M6).

---

## Step 5 — Unknown (b): can the panel view be captured?

By contract the loop NEVER captures on XR (`hasScreenshot=false`). This step empirically answers whether capture is even POSSIBLE, which SETS whether `hasScreenshot` could ever be true on XR.

```bash
# Rough framebuffer proxy (NOT identical to AccessibilityService.takeScreenshot, but a strong signal):
adb shell screencap -p /sdcard/xr_probe.png
adb pull /sdcard/xr_probe.png ./xr_probe.png
# inspect ./xr_probe.png: is the 2D panel visible, or black / passthrough-protected / an error?
```

For the definitive answer, use a **debug probe build** that calls `AccessibilityService.takeScreenshot(displayId, ...)` (and/or MediaProjection) against the panel's display and logs success + whether the panel pixels are present vs. black.

**Expected:** UNKNOWN — this is the finding. Likely outcomes: (i) black/blank frame (compositor-protected) → `hasScreenshot=false` stands; (ii) panel captured → a future flag flip could enable vision on XR.
**How to record:** attach/describe `xr_probe.png` (panel visible? black? error?) + any `takeScreenshot` log from the probe build.
**Contract meaning — resolves Unknown (b):** if capture is impossible/black, the current `screenshotAvailable(XR)=false` is validated and the tree-only loop is the correct permanent design. If capture works, file a follow-up to allow `hasScreenshot=true` on XR (the loop already consumes a screenshot when present — no loop change needed, only the capability flag).

---

## Step 6 — Does an intent (`open_app`) land in a spatial panel?

```bash
curl -s -X POST "http://$XR:$PORT/action" -H 'Content-Type: application/json' -d '{
  "msg":"action","task_id":"probe3","operator":"'"$OP"'",
  "type":"open_app","package":"com.android.settings"
}' | python3 -m json.tool
```

**Expected:** `{"success":true,"detail":"launched com.android.settings"}` **and** Settings opens as a spatial panel in the headset. Try 2–3 packages (a Google app, a sideloaded app). Also try `open_url` (ACTION_VIEW) and one common intent (e.g. `set_timer`) if apps are present.
**How to record:** paste each `action_result` + note whether the app actually appeared as a panel.
**Contract meaning:** intents fire through the Application context (NO accessibility needed), so this is the most XR-portable path. If `open_app` launches panels reliably, the intent layer is the backbone of XR control (as designed). Package-not-found → check Android 11+ `<queries>` visibility for that package.

---

## Step 7 — Full `control_device` task via node+intent only

From the box (Portal chat, MCP, or a direct tool call), run a real multi-step task targeting the headset, e.g.:

> "On my Galaxy XR, open Settings and turn on Bluetooth."

Target the headset explicitly if needed (`device=$XR`); otherwise origin/primary routing applies (M3).

**Expected:** the server-side frontier loop completes the task using **element_click / element_set_text / open_app / global_action / press_key** frames ONLY — **zero** `coordinate_*` frames, **zero** screenshots requested. The in-headset **consent surface** is visible for the duration; pressing **STOP** halts actuation immediately.

> **Where the STOP lives (tester expectation):** the in-headset STOP button is in the **EXPANDED panel** ("AI is controlling this device" banner + STOP). When the overlay is **collapsed**, the bubble shows a red **CTRL** chip instead — expand it to reach STOP. The **STOP notification** (`NotificationListenerFgs`) is the always-on fail-safe and does NOT require expanding the panel. So verify STOP via BOTH: (i) expand the panel → tap STOP; (ii) the STOP notification in the XR shade.

**How to record:**
- Note task success/failure + step count (`data.steps`).
- On the box, `journalctl`-tail during the run and confirm the posted frames are element/intent/global only (no `coordinate_tap`/`coordinate_swipe`).
- Confirm the banner appeared and STOP aborted (fire STOP mid-task once to verify: subsequent frames must be refused with "stopped by user").

**Contract meaning:** end-to-end proof that node+intent+capture-independent control drives a real XR task, with the consent+kill surface working in-headset.

---

## Findings log (fill in on-device)

| # | Check | Expected | Actual | Pass? |
|---|---|---|---|---|
| 0 | Prereqs (tailnet, operator bound) | all true | | |
| 1 | a11y service enabled | contains `BlackBoxA11yService` | | |
| 2 | capability contract | `xr_headset` / coord false / shot false / no `screenshot` key | | |
| 3 | **Unknown (a)** node `ACTION_CLICK` actuates panel | success + panel reacts | | |
| 4 | coordinate_tap skipped (gate) | `invalid_argument` "not supported on xr_headset" | | |
| 4b | (opt) raw `dispatchGesture` on panel | no-op (expected) | | |
| 5 | **Unknown (b)** panel capture possible? | UNKNOWN → record | | |
| 6 | `open_app` intent lands panel | success + panel appears | | |
| 7 | full `control_device` node+intent task | completes, banner+STOP work, no coord/shot | | |

---

## What CANNOT be validated without the Galaxy XR (for Brandon)

Everything below is UNVERIFIABLE off-device — it needs the headset in hand. The build is fail-safe regardless (capability-flag degradation), but these are the open items:

1. **Unknown (a) — panel actuation:** whether `ACTION_CLICK` (and, empirically, `dispatchGesture`) actually moves a 2D app panel inside the XR compositor. Unit tests prove the WIRE/routing; only the headset proves the pixels move (steps 3–4).
2. **Unknown (b) — panel capture:** whether `takeScreenshot()`/MediaProjection can grab the composited panel at all (step 5). This is the single input that decides if `hasScreenshot` could ever be true on XR. Currently hard-coded false (the safe assumption).
3. **XR probe correctness:** whether this specific Galaxy XR reports `android.software.xr.api.spatial` / `UI_MODE_TYPE_VR_HEADSET` so `detect()` classifies it `xr_headset` (step 2). If not, `DeviceCapabilities.XR_SYSTEM_FEATURES` needs the real feature string from the device.
4. **In-headset consent UX:** whether the `XrExpandedPanel` banner + STOP render legibly and are reachable in the spatial UI, and whether the STOP notification is visible in the XR notification shade (steps 1, 7). The wiring is unit-tested; the visual/reachability is device-only.
5. **`open_app` / intent panel behavior:** whether launching apps via intent reliably produces spatial panels and multi-panel scenes behave (step 6).
6. **Scroll on XR:** `scroll` is currently implemented as a `dispatchGesture` swipe (not coordinate-gated, since it is `ActionKind.SCROLL`, not `COORDINATE`). On XR that gesture MAY be a no-op — step 7 should exercise a scroll and, if it fails, the model re-plans (graceful). If scroll is confirmed dead on XR, a follow-up can add a node-based `ACTION_SCROLL_FORWARD/BACKWARD` path (out of M6 scope, needs hardware to validate).
7. **Latency/stability of the 8765 SSE loop over the headset's radio** under a real multi-step task (step 7).
