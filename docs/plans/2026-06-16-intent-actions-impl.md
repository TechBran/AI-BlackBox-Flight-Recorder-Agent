# Intent-based common actions — implementation plan

> **For Claude:** Execute via superpowers:subagent-driven-development (implementer →
> spec review → code-quality review per task). Branch `feat/local-gemma-impl`,
> worktree only. Explicit-path commits. `Co-Authored-By: Claude Opus 4.8`.
> Supersedes Priority 1 of `2026-06-16-intent-actions-and-phase4-followups.md`.

**Goal:** Give the on-device Gemma agent deterministic, single-shot "do a common
phone task" tools that fire OS Intents (no screen-read, no node_id, no chaining) —
Google "Mobile Actions" parity (the 7) + our expansion (9 more). This is the
reliability fix: one model tool call → one Intent → done.

**Architecture:** Pure URI/extras BUILDERS (JVM-unit-testable) + a framework
`IntentActuator` (build+fire, device-verified), routed LOCALLY through the existing
`PhoneController` seam exactly like the gesture actuators, gated for the few
high-consequence intents through the existing `ConfirmGate`.

**Tech stack:** Kotlin, AccessibilityService-as-Context, `startActivity`,
`CameraManager.setTorchMode` (flashlight), kotlinx.serialization schemas, JUnit host tests.

---

## GROUND TRUTH — Google AI Edge Gallery "Mobile Actions" (verbatim, fetched 2026-06-16)

Source: `github.com/google-ai-edge/gallery` …/customtasks/mobileactions/. Model:
`functiongemma-270m-it` (Gemma-3 arch). The `@Tool` fn only *records* an Action +
returns `mapOf("result" to "success", …)`; `performAction()` fires the Intent later.
**The 7 actions and their EXACT Intent construction:**

1. **turnOnFlashlight()** / **turnOffFlashlight()** — NOT an Intent.
   `CameraManager.setTorchMode(cameraId, isEnabled)` where `cameraId` = first id in
   `cameraManager.cameraIdList` whose `CameraCharacteristics.FLASH_INFO_AVAILABLE == true`.
2. **createContact(firstName, lastName, phoneNumber, email)** →
   `Intent(ContactsContract.Intents.Insert.ACTION)` with
   `type = ContactsContract.RawContacts.CONTENT_TYPE`, extras:
   `Insert.NAME = "$firstName $lastName"`, `Insert.EMAIL = email`,
   `Insert.EMAIL_TYPE = CommonDataKinds.Email.TYPE_WORK`, `Insert.PHONE = phoneNumber`,
   `Insert.PHONE_TYPE = CommonDataKinds.Phone.TYPE_WORK`.
3. **sendEmail(to, subject, body)** → `Intent(Intent.ACTION_SEND)`, `data = "mailto:".toUri()`,
   `type = "text/plain"`, `EXTRA_EMAIL = arrayOf(to)`, `EXTRA_SUBJECT = subject`, `EXTRA_TEXT = body`.
4. **showLocationOnMap(location)** → `Intent(Intent.ACTION_VIEW)`,
   `data = "geo:0,0?q=${URLEncoder.encode(location, UTF_8)}".toUri()`.
5. **openWifiSettings()** → `Intent(Settings.ACTION_WIFI_SETTINGS)`.
6. **createCalendarEvent(datetime, title)** → `Intent(Intent.ACTION_INSERT)`,
   `data = CalendarContract.Events.CONTENT_URI`, `Events.TITLE = title`,
   `EXTRA_EVENT_BEGIN_TIME = ms`, `EXTRA_EVENT_END_TIME = ms + 3600000`. `ms` from
   `LocalDateTime.parse(datetime).atZone(systemDefault()).toInstant().toEpochMilli()`,
   fallback `System.currentTimeMillis()` on parse failure. datetime fmt `YYYY-MM-DDTHH:MM:SS`.

**System prompt (getSystemPrompt()):** two Content.Text lines —
`"You are a model that can do function calling with the following functions"` +
`"Current date and time given in YYYY-MM-DDTHH:MM:SS format: <now>\nDay of week is <EEEE>"`.
(Carry to Priority 2: our `/local/system-prompt` should inject current date/time too —
calendar/alarm need it.) From the background service every `startActivity` needs
`FLAG_ACTIVITY_NEW_TASK` (Gallery runs from an Activity so it doesn't; we DO — cf.
`Actuators.openApp` which already adds it).

---

## Our seams (already on branch — do NOT re-read; signatures here)

- `ActuatorResult(val success: Boolean, val detail: String)` (Actuators.kt).
- `Actuators(service: () -> BlackBoxA11yService?, mode: () -> AutonomyMode = {YOLO},
  confirm: ConfirmUi = AutoApproveConfirmUi, credentialHandoff = AutoDecline…)`.
  Pattern: `svc.startActivity(intent.addFlags(FLAG_ACTIVITY_NEW_TASK))`,
  `svc.packageManager`, `svc.getSystemService(...)`. `BlackBoxA11yService.instance`
  is the live Context seam (a Service IS a Context).
- `ConfirmUi.confirm(description: String): Boolean` (suspend). `AutonomyMode{PERMISSION,YOLO}`.
  Pure gate core in ConfirmGate.kt: `isHighConsequence(action,label,isPwd)`,
  `shouldConfirm(mode,hc)`, `describeAction(action,label)`.
- `PhoneController.dispatch(name: String, args: JsonObject): ToolResult` (suspend, never throws).
  Prod impl `AndroidPhoneController(reader, actuators)`; factory
  `AndroidPhoneController.fromService(mode, confirm, credentialHandoff)`.
- `ResidentTools`: `PHONE_ACTUATORS: Set<String>`, `phoneActuators(): List<ToolSchema>`,
  `MAX_INJECTED_SCHEMAS=5`. `ToolSchema(name, description, parameters: JsonObject)`.
- `FcLoop.runAgent`: routes `if (phone != null && call.name in ResidentTools.PHONE_ACTUATORS)`
  → `phone.dispatch(...)` + `continue` (never the cloud bridge). Tool list
  `available = (resident + phoneTools + injected).distinctBy{name}`, ORDER LOAD-BEARING.
  `phoneTools = if (phone != null) ResidentTools.phoneActuators() else emptyList()`.

---

## The full action set (16) — names = dispatch keys = schema names (snake_case)

**Google parity (7):** `flashlight_on`, `flashlight_off`, `create_contact`,
`send_email`, `show_map`, `open_wifi_settings`, `create_calendar_event`.

**Expansion (9):**
- `open_url(url)` → `ACTION_VIEW`, `data = url` (http/https only — pure validate; reject else).
- `dial(number)` → `ACTION_DIAL`, `data = "tel:$number".toUri()`. (Opens dialer prefilled;
  user taps call. NO `ACTION_CALL` — that needs CALL_PHONE runtime perm + auto-places.)
- `send_sms(number, body)` → `ACTION_SENDTO`, `data = "smsto:$number".toUri()`,
  `putExtra("sms_body", body)`. (Opens messaging prefilled; user taps send.)
- `set_alarm(hour, minutes, label)` → `AlarmClock.ACTION_SET_ALARM`,
  `EXTRA_HOUR`, `EXTRA_MINUTES`, `EXTRA_MESSAGE = label`, `EXTRA_SKIP_UI = false`.
- `set_timer(seconds, label)` → `AlarmClock.ACTION_SET_TIMER`, `EXTRA_LENGTH = seconds`,
  `EXTRA_MESSAGE = label`, `EXTRA_SKIP_UI = false`.
- `share_text(text)` → `Intent.createChooser(Intent(ACTION_SEND){type="text/plain";
  EXTRA_TEXT=text}, null)`. (User picks target + confirms.)
- `open_settings_panel(which)` → `Intent(<settings action for which>)`. Pure mapper
  `which → Settings.ACTION_*`: wifi→ACTION_WIFI_SETTINGS, bluetooth→ACTION_BLUETOOTH_SETTINGS,
  location→ACTION_LOCATION_SOURCE_SETTINGS, sound→ACTION_SOUND_SETTINGS,
  display→ACTION_DISPLAY_SETTINGS, battery→ACTION_BATTERY_SAVER_SETTINGS,
  nfc→ACTION_NFC_SETTINGS, airplane→ACTION_AIRPLANE_MODE_SETTINGS,
  data/cellular→ACTION_DATA_ROAMING_SETTINGS, storage→ACTION_INTERNAL_STORAGE_SETTINGS,
  apps→ACTION_APPLICATION_SETTINGS, settings/null/unknown→ACTION_SETTINGS (fallback).
- `take_photo()` → `MediaStore.ACTION_IMAGE_CAPTURE`. (Launches camera.)
- `web_search(query)` → `Intent(ACTION_WEB_SEARCH)`, `putExtra(SearchManager.QUERY, query)`.

**High-consequence (gate in PERMISSION mode):** `send_email`, `send_sms`. Everything
else is benign (user has a final tap in the launched UI, or it's a settings/torch/search).
Extensible set — unit-tested.

---

## Tasks

### Task IA-1: Pure builders + pure intent-gate decisions (+ tests) — NO framework
**Files:**
- Create `…/overlay/IntentActions.kt` — pure top-level fns only (no `Intent`, no android
  method calls; android `static final String` constants are OK — they inline).
- Modify `…/overlay/ConfirmGate.kt` — add pure `isHighConsequenceIntent(name)`,
  `shouldConfirmIntent(mode, name)`, `describeIntent(name, primaryArg)`.
- Create `…/test/…/overlay/IntentActionsTest.kt`, `IntentGateTest.kt`.

Pure fns (exact): `geoQueryUri(query): String` = `"geo:0,0?q=" + URLEncoder.encode(query,"UTF-8")`;
`telUri(number): String` = `"tel:" + number.filter{it.isDigit()||it in "+*#"}`;
`smsToUri(number)` likewise with `"smsto:"`; `isWebUrl(url): Boolean` (http/https, has host);
`calendarMillis(datetime, nowMs): Long` (LocalDateTime.parse → epoch ms; fallback nowMs);
`calendarEndMillis(beginMs) = beginMs + 3_600_000`; `settingsPanelAction(which: String?): String`
(the mapping above; returns the constant String, never null — unknown→ACTION_SETTINGS);
`clampHour/clampMinutes/clampTimerSeconds` (sane bounds). Gate: high-consequence set
`setOf("send_email","send_sms")`; `describeIntent` e.g. `send_email`→`Send an email to "<to>"`,
`send_sms`→`Send a text to "<number>"` (NEVER include the body). Tests: every builder
(incl. URL-encoding of spaces/`&`, tel sanitization, calendar valid+invalid, each settings
`which` + unknown), and the gate for every action name × both modes.

### Task IA-2: IntentActuator framework class (build + fire, gate internally)
**Files:** Create `…/overlay/IntentActuator.kt`.
`class IntentActuator(service: () -> BlackBoxA11yService?, mode: () -> AutonomyMode = {YOLO},
confirm: ConfirmUi = AutoApproveConfirmUi)`. One `suspend fun perform(name, args: JsonObject):
ActuatorResult` (or one fn per action + a `when`). Uses IA-1 pure helpers to build each
Intent, `addFlags(FLAG_ACTIVITY_NEW_TASK)`, `svc.startActivity(...)` in try/catch →
`ActuatorResult(false, "<name> failed (<ExClass>)")` on `ActivityNotFoundException`/any.
Flashlight via `svc.getSystemService(CAMERA_SERVICE) as CameraManager` + `setTorchMode`.
GATE high-consequence intents: `if (shouldConfirmIntent(mode(), name) && !confirm.confirm(
describeIntent(name, primaryArg))) return ActuatorResult(false, "user declined")` BEFORE
firing. NEVER log the body/text/email/sms args (leak discipline — only the action name).
`companion fun fromService(mode, confirm)`. Framework → device-verified (like Actuators).

### Task IA-3: Wire into ResidentTools + AndroidPhoneController + FcLoop (+ routing tests)
**Files:** Modify `…/data/local/ResidentTools.kt`, `…/overlay/AndroidPhoneController.kt`,
`…/data/local/FcLoop.kt`; add `…/test/…` routing tests.
- ResidentTools: `INTENT_ACTIONS: Set<String>` (the 16), `LOCAL_PHONE_TOOLS =
  PHONE_ACTUATORS + INTENT_ACTIONS`, `intentActions(): List<ToolSchema>` (terse,
  reliability-steering descriptions: "Prefer this over read_screen/tap for …").
- AndroidPhoneController: hold an `IntentActuator`; in `dispatch`, `else if (name in
  ResidentTools.INTENT_ACTIONS) intentActuator.perform(name, args).toToolResult()`.
  `fromService(mode, confirm, credentialHandoff)` also builds `IntentActuator.fromService(
  mode, confirm)`.
- FcLoop: route on `ResidentTools.LOCAL_PHONE_TOOLS` (not just PHONE_ACTUATORS);
  `phoneTools = phoneActuators() + intentActions()` when `phone != null`. Keep order comment.
- Tests: schema presence/shape; FcLoop routes an intent name through the fake PhoneController
  (never the bridge); AndroidPhoneController routes an intent name to a fake IntentActuator
  and a gesture name to the actuators (extend existing pattern).

### Task IA-4: Manifest (`<queries>` + SET_ALARM) + full build + unit-test green
**Files:** Modify `…/app/src/main/AndroidManifest.xml`.
- `<uses-permission android:name="com.android.alarm.permission.SET_ALARM"/>`.
- `<queries>` for implicit-intent resolvability: intents/schemes for tel, smsto, mailto,
  geo, https, ACTION_INSERT (contacts+calendar), ACTION_IMAGE_CAPTURE, ACTION_WEB_SEARCH,
  ACTION_SENDTO, ACTION_SEND text/plain. Then
  `./gradlew :app:testDebugUnitTest --offline` green; `assembleDebug` builds.

### After all tasks
Final whole-feature code review → device-verify `flashlight_on`/`flashlight_off` (torch
visibly toggles) + `show_map` ("navigate to …" → Maps opens) on the Fold 6 → snapshot.

## Carry-forward
- Per-turn tool count rises to ~25 resident (1 search + 8 gesture + 16 intent). Measure
  E4B selection accuracy; if it degrades, tier/trim in Priority 2 (tool-ordering tuning).
- Priority 2: inject current date/time into `/local/system-prompt` (calendar/alarm need it),
  per Google's getSystemPrompt.
