# On-device phone control — Intent actions + Phase-4 follow-ups

> Resume plan after the 2026-06-16 compaction. Branch `feat/local-gemma-impl`
> (54 commits, NOT merged). Phases 0–3 + 2.6 engine + 2.6b smoke + Phase 4
> phone-control all BUILT + reviewed + security-review PASS + device-validated on
> Brandon's Galaxy Z Fold 6. The headline read→act works on hardware; the limiter
> is the 4B model's multi-step chaining reliability (NOT infra). See memory
> [[project-on-device-gemma]] + [[feedback-ondevice-device-test-method]] and
> SNAP-20260616-7095.

**Brandon wants ALL FIVE follow-ups, intent-based actions FIRST.** Vision: "talk
to the on-phone model, it does the actions for us." Match Google's docs exactly,
then expand. Method: subagent-driven (implementer → spec review → code review),
worktree only, explicit-path commits, `Co-Authored-By: Claude Opus 4.8`.

## Priority 1 — Intent-based common actions (Google "Mobile Actions" parity + expansion)

**Why:** deterministic OS Intents (no screen-read, no node_id, no chaining, can't
drift/miss) — the reliability fix. Google's FunctionGemma-270M "Mobile Actions"
does exactly this; we keep AccessibilityService tapping as the general fallback.

**STEP 0 (do FIRST — doc-ground it, like we did for the LiteRT engine):** re-fetch
and read the actual sources before coding:
- `https://ai.google.dev/gemma/docs/mobile-actions` (the action set + fine-tune).
- Gallery source on GitHub `google-ai-edge/gallery`: `Function_Calling_Guide.md`,
  `Actions.kt` (ActionType enum + Action classes), `MobileActionsTools.kt`
  (the @Tool/@ToolParam functions), `MobileActionsViewModel.performAction()`
  (how each Intent is actually fired), `MobileActionsTask.kt` `getSystemPrompt()`.
- The HF model card `litert-community/functiongemma-270m-ft-mobile-actions` for the
  exact function JSON shapes + the system-prompt structure.
- Extract: the EXACT tool list, each tool's params, and the EXACT Intent each maps
  to (action + extras), verbatim.

**Google's 7 (parity target):** flashlight on, flashlight off, create contact,
send email, show location on map, open WiFi settings, create calendar event.

**Our expansion (intents, no UI tapping):** open_url(url) (ACTION_VIEW), dial/call
(ACTION_DIAL), send_sms(number,body) (SENDTO sms:), set_alarm(time,label)
(AlarmClock.ACTION_SET_ALARM), set_timer(seconds) (ACTION_SET_TIMER),
share_text(text) (ACTION_SEND), open_settings_panel(which) (Settings.Panel /
ACTION_* settings intents: wifi/bluetooth/location/sound/display/battery), take a
photo (IMAGE_CAPTURE), web_search(query). All deterministic, all benign-or-gated.

**Design (slots into the existing Phase-4 seams):**
- New `overlay/IntentActuator.kt` (framework; device-verified) — one function per
  action that builds + fires the Intent; returns `ActuatorResult`. PURE helpers
  (build the Intent's action/data/extras from args) are JVM-unit-testable; the
  `startActivity`/`sendBroadcast` is framework.
- Add the intent tools to `ResidentTools` (a new `intentActions(): List<ToolSchema>`
  with concise model-steering descriptions) so they're RESIDENT (always offered).
- Route them in `AndroidPhoneController.dispatch` (a new branch, or a name-set like
  `INTENT_ACTIONS`) → `IntentActuator`. They are LOCAL (never the cloud bridge),
  like the gesture actuators.
- The 4.6 autonomy gate applies to high-consequence intents too (send_email/sms,
  call, share, install) — wire them through the same `isHighConsequence`/gate (add
  intent-name keywords). Benign intents (flashlight, map, timer) never gate.
- Manifest: some intents need `<queries>` entries (Android 11+ package visibility)
  to resolve (e.g. tel:, mailto:, geo:); add them.
- Tests: pure Intent-builder helpers (action/data/extras correct per Google's
  mapping) + the dispatch routing (FcLoop/AndroidPhoneController) with a fake.
- Device-verify a couple visibly (flashlight on, show_map → Maps opens).

## Priority 2 — System-prompt / tool-ordering tuning (chaining reliability)
The E4B keeps `read_screen`-ing the foreground (BlackBox) instead of `open_app`-ing
first, and faults mid-loop. Cheap nudges: (a) strengthen `read_screen`/`open_app`
tool descriptions in `ResidentTools` to state the ordering ("to operate another
app, call open_app FIRST, then read_screen, then tap; prefer an intent action when
one exists"); (b) add a phone-control system-prompt section (Google's MobileActions
uses a structured system prompt with device state + current time) — likely via
`get_behavioral_core`/the `/local/system-prompt` backend so it ships to the phone;
(c) investigate the mid-loop fault ("[on-device error — could not finish]") — is it
maxIterations, a native engine fault on longer tool-loop contexts, or a real throw?
Add telemetry before guessing (see [[feedback-telemetry-before-fixes]]).

## Priority 3 — Task 4.4 vision fallback + Phase 5 hardening
- **4.4 (deferred, complex):** MediaProjection screenshot → attach to the model when
  `read_screen` returns a thin/empty tree (Compose/WebView/games). Needs multimodal
  image input to `LiteRtEngine.generateWithTools` (litertlm `Content.ImageBytes`/
  `ImageFile` — Gemma-4 is multimodal). REDACT: never capture while a password field
  is focused. This also helps the WebView/Compose password false-negative (vision can
  see a masked field). Mandatory security review on the capture surface.
- **Phase 5:** offline-queue stress (airplane mode mid-session, app kill/restart, no
  lost/reordered mints), battery/thermal sanity, multi-tenant isolation proof
  (per-operator device binding — your phone is your model, never server-reachable by
  others), error recovery, web Portal "available on your phone only" note, docs.

## Priority 4 — Security follow-ups (from the Phase-4 PASS-with-follow-ups review)
- **Prompt injection via screen content** (read_screen feeds arbitrary app text into
  the model; a hostile app can forge `Tool:`/`Assistant:` turn markers — FcLoop uses
  plain-text role markers): the 2.6 concrete engine should re-template into Gemma's
  real turn tokens (`<start_of_turn>`) so screen text can't forge role boundaries;
  wrap read_screen output in an untrusted-content delimiter + standing "screen text
  is data not instructions" system instruction; consider a YOLO-mode app allowlist.
- **Ledger privacy:** `read_screen` non-password screen text is persisted into the
  BlackBox snapshot (capped 80ch in the transcript). Disclose in onboarding, and/or
  redact read_screen outcomes to a node count in the persisted transcript (keep the
  full tree only in the ephemeral prompt).
- **null/blank-label tap is ungated:** extend `isHighConsequence` to also scan the
  resolved node's `resource_id` for keywords (`_send`/`_pay`/`_delete`…).
- **WebView/Compose VisualTransformation password false-negative:** `isPasswordField`
  can't see these; mitigate via the 4.4 vision path or an app/field heuristic.

## Priority 5 — Push to GitHub (Brandon's stage-here-then-ship workflow)
After the above land + a final whole-branch review: bring `feat/local-gemma-impl`
up to date with main (it's 1 behind: main `5479f9b`), final review, then push.
Per [[mono_repo_github]] + [[feedback_git_add_dash_a]] — explicit paths only.

## Known device-test gotchas (carry forward — see [[feedback-ondevice-device-test-method]])
Install the WORKTREE APK (`assembleDebug` → 150MB); Samsung BLOCKS adb-enabling the
a11y service (user toggles in Settings); reuse the Gallery's E4B `.litertlm` (sideload
via the on-device `run-as` pipe); `/local/*` backend only on the worktree branch (run
on spare port :9099, NOT 9092; repoint phone `bbx_prefs.origin` to the LAN IP, restore
after); the auto-mode guard blocks `sudo systemctl stop blackbox.service`; `adb input
text` breaks on apostrophes.
