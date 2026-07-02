# Device-Control Deployment (M8.5 — sideload / enterprise only)

**Status:** ship-ready · **Distribution:** SIDELOAD (adb) + MDM/enterprise ONLY · **NEVER Google Play.**

This is the deployment guide for the frontier-driven device-control feature (the AI BlackBox
Android app drives the user's own phone/tablet/XR device over Tailscale, with the cloud frontier
model as the "brain" and the phone's accessibility + intent layer as the "hands"). It covers the
signed build, the manual grants the customer must make, and the Play-policy / Advanced-Protection
caveats that make this a sideload/enterprise product.

---

## 1. Why sideload / enterprise only (NOT Google Play)

Google Play **prohibits** using the AccessibilityService API to build a general-purpose
device-automation / remote-control agent that acts on the user's behalf across other apps. Our app
is exactly that: a consented assistant that reads the screen and taps/types **for the user, at the
user's request**. Therefore:

- **We do NOT declare `android:isAccessibilityTool="true"`** in `res/xml/accessibility_service_config.xml`.
  That flag asserts "this service's PRIMARY purpose is to assist users with disabilities," which is
  **not** our purpose — claiming it to pass Play review would be a misrepresentation. Our service is
  an honest assistant/automation service, so the flag is deliberately **absent** (verified: the M8
  build asserts it is not present).
- **We never submit to Google Play.** The build has no Play upload/publish path. Distribute the
  signed APK by **adb sideload** (individual devices) or **MDM / enterprise app management**
  (managed fleets — Android Enterprise "private app" / OEM MDM).
- Package visibility uses a **broad-but-honest `<queries>`** (LAUNCHER + BROWSABLE http/https +
  the concrete intents the actuator fires) rather than the Play-sensitive `QUERY_ALL_PACKAGES`.

---

## 2. Build a signed release APK

The release build type is signed from **environment / Gradle properties** — **no keystore is
committed**. If the enterprise keystore variables are unset, the build **falls back to debug
signing** (buildable/testable, but **do not distribute** a debug-signed APK).

### 2.1 Provide the enterprise keystore (recommended for distribution)

Set these in `~/.gradle/gradle.properties` (never in a committed file) **or** the CI/MDM env:

```properties
BLACKBOX_KEYSTORE_FILE=/secure/path/blackbox-enterprise.jks
BLACKBOX_KEYSTORE_PASSWORD=********
BLACKBOX_KEY_ALIAS=blackbox
BLACKBOX_KEY_PASSWORD=********
```

Generate an enterprise key once (keep it OFFLINE / in a secret store — it identifies every future
upgrade of the installed app):

```bash
keytool -genkeypair -v -keystore blackbox-enterprise.jks -alias blackbox \
  -keyalg RSA -keysize 4096 -validity 10000
```

### 2.2 Build

```bash
cd "AI_BlackBox_Portal_Android_MVP (2)/AI_BlackBox_Portal_Android_MVP/AI_BlackBox_Portal"
./gradlew :app:assembleRelease
# → app/build/outputs/apk/release/app-release.apk  (enterprise-signed when the keystore vars are set;
#   otherwise debug-signed with a "do not distribute" warning in the build log)
```

Bump `versionCode` in `app/build.gradle` on **every** distributed change so installed APKs upgrade
over the top without an uninstall.

### 2.3 RELEASE SIGNING HARD GATE (required for any distributable build) 🚧

**A distributable release MUST be enterprise-signed — `BLACKBOX_KEYSTORE_*` MUST be set.** The
debug-signed fallback (§2, when the keystore vars are unset) exists ONLY so a fresh box / CI can
still *build* the release variant; a debug-signed APK is **dev-only and must NEVER be distributed**
(same debug key on every machine, non-upgradable, untrusted by MDM).

To make this a **hard failure** in the release / CI pipeline (so it can never silently ship a
debug-signed APK), pass the opt-in flag — with it set and no keystore configured, `assembleRelease`
**fails** instead of falling back to debug signing:

```bash
# release / distribution pipeline — FAILS unless BLACKBOX_KEYSTORE_* is configured:
./gradlew :app:assembleRelease -PrequireReleaseSigning
# (equivalently: export BLACKBOX_REQUIRE_RELEASE_SIGNING=1)
```

Local/dev builds omit the flag and keep the buildable debug-signed fallback. **Never distribute an
APK produced without the enterprise keystore.**

---

## 3. Sideload to a single device (adb)

```bash
adb install -r app/build/outputs/apk/release/app-release.apk
# -r = reinstall/upgrade in place (keeps app data; requires the same signing key)
```

For a fresh install, first enable Developer Options → USB debugging on the device.

---

## 4. Enterprise / MDM deployment (managed fleets)

Publish the signed APK as a **private app** in your MDM / Android Enterprise console (managed Google
Play private app, or an OEM/EMM app-push), targeted to the managed device group. The MDM can also
**pre-grant** some of the runtime permissions below on managed devices, but the three
**system-toggle** grants in §5 (accessibility, display-over-other-apps, notification access) still
require an explicit user (or managed-config) action per Android policy.

---

## 5. Required manual grants (per device, after install)

Device control is **opt-in and revocable**. The user (or MDM managed config) must enable, in order:

1. **Enable BlackBox accessibility** — Settings → Accessibility → **BlackBox** (the
   `BlackBoxA11yService`) → On. This grants screen reading + gesture actuation (tap/type/swipe/
   scroll) + the silent screenshot. **Without it the app runs in `intent_only_mode`** (see §6).
2. **Display over other apps** (`SYSTEM_ALERT_WINDOW`) — Settings → Apps → BlackBox → Display over
   other apps → Allow. Required for the on-device **confirm gate** (high-consequence Allow/Deny
   prompts) + the "AI is controlling this device" **consent banner / kill switch**. The confirm
   gate **fails safe to DENY** if this permission is missing.
3. **Notification access / posting** — grant `POST_NOTIFICATIONS` (Android 13+) so the fail-safe
   STOP notification and inbound `/notify` work even when the overlay banner can't show. (Enabling
   the **notification listener** is only needed for notification mirroring features, not for
   device control itself.)

All three are user-revocable at any time; revoking accessibility triggers the graceful degradation
in §6.

---

## 6. Advanced Protection / a11y revocation → graceful intent fallback (M8.1)

Android **Advanced Protection Mode** (and a user toggle) can **OS-revoke** the AccessibilityService.
The app degrades gracefully rather than crashing:

- Screen actions (`read_screen` / `tap` / `type` / `swipe` / `scroll` / gestures / global back-home-
  recents) return a clear **`intent_only_mode`** result that lists the still-available intent
  actions. The device capability advertised on the wire also flips `accessibilityEnabled=false`
  (and `hasScreenshot=false` / `supportsCoordinateGesture=false`), so the cloud loop knows.
- The **intent path keeps working** — `open_url`, `show_map`, `dial`, `send_sms`, `open_settings`,
  the alarm/calendar/camera/contacts/file/media catalog — because those fire through the
  Application `Context`, needing **no accessibility**.
- **Re-enabling accessibility resumes** tree reading + gesture actuation on the very next action
  (the a11y state is probed live per dispatch).

The cloud frontier loop, which emits screen actions (not intent actions), surfaces this as a clean
terminal `intent_only` with a "re-enable BlackBox accessibility" message — it never spins or
crashes.

---

## 7. Incident kill switch (M8.2)

Every remote session can be stopped instantly, three ways:

- **On-device:** the consent-banner **STOP** button, or the ongoing "AI is controlling this device"
  notification's **STOP** action (works even with no overlay permission).
- **Remotely (operator):** the `stop_device_control` tool (or `control_device(action="stop")`),
  which reaches the phone's operator-scoped `POST /kill-all` (or `POST /kill/{taskId}`). A killed
  task is refused on every subsequent `/action` + `/stream` frame and its banner drops.

---

## 8. Telemetry & privacy (M8.3)

Per-step telemetry (action name, success, latency, capture kind) is recorded in a small on-device
**SQLite** store (`databases/blackbox_remote_telemetry.db`, single `remote_step` table) so it
**survives a foreground-service restart / process kill**. It is **retention-bounded** — rows older
than **7 days** are pruned on every write and the table is capped to the newest 512 rows — and
readable only over the operator-scoped `GET /telemetry/{taskId}` / `GET /telemetry/summary?operator=`.
**No screen text, typed text, node content, coordinates, or action arguments are ever stored or
transmitted in telemetry or logs** — the store columns mirror the non-sensitive sink signature
exactly, so there is no field a secret could occupy.

---

## 9. Distribution checklist

- [ ] `versionCode` bumped.
- [ ] **RELEASE SIGNING HARD GATE (§2.3):** `BLACKBOX_KEYSTORE_*` set → `assembleRelease` produces an
      **enterprise-signed** APK (build log does NOT show the debug-signed warning). Build the
      distributable with `-PrequireReleaseSigning` so an unset keystore **fails the build** rather
      than shipping a debug-signed APK. **Never distribute a debug-signed (fallback) APK.**
- [ ] `res/xml/accessibility_service_config.xml` has **no `android:isAccessibilityTool`**.
- [ ] Distributed via **adb sideload** or **MDM private app** — **never** submitted to Google Play.
- [ ] Device grants documented for the customer (accessibility, display-over-apps, notifications).
