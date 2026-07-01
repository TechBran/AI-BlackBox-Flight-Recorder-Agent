package com.aiblackbox.portal.overlay

import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.net.Uri
import android.provider.AlarmClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.provider.MediaStore
import android.provider.Settings
import android.util.Log
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonPrimitive

/**
 * The framework ACTUATOR for the on-device "intent-based phone actions" feature
 * (Task IA-2). It is the counterpart to the PURE argument builders in
 * [IntentActions] (Task IA-1): given a named intent action + the JSON args the
 * on-device Gemma agent emitted, it BUILDS the stock Android [Intent] (reusing the
 * IA-1 pure helpers for every risky argument transform) and FIRES it via
 * `startActivity` — or, for the flashlight, drives [CameraManager.setTorchMode]
 * directly (the torch is not an intent).
 *
 * An *intent action* is a benign, well-bounded request the agent satisfies by
 * handing off to a stock app: show a place on a map, dial a number, draft an
 * SMS/email, open a settings panel, set a timer/alarm, take a photo, share text,
 * create a contact/calendar event. The argument sanitization
 * (the actual safety surface — `tel:` smuggling, non-web `open_url`, `geo:` query
 * break-out, hour/minute/second clamps) all lives in the host-JVM-tested IA-1
 * helpers; THIS class only assembles + launches, so it is framework code,
 * **device-verified (not unit-tested here)** — exactly like [Actuators].
 *
 * ## Fires via the Application context — needs ZERO accessibility (Gallery parity)
 * Every intent here is launched through the process-wide **Application** [Context]
 * (via [context]), NOT the accessibility service. None of these benign hand-offs
 * (map, dial, settings, timer, …) require any accessibility capability, so this
 * actuator works with the [BlackBoxA11yService] DISABLED — matching Google's Edge
 * Gallery, which fires its intents from a plain app Context and uses no
 * accessibility at all. Accessibility remains required ONLY for the gesture layer
 * ([UiTreeReader]/[Actuators]: read_screen, tap, type, swipe, …); the intent layer
 * is fully decoupled from it.
 *
 * ## Result, not exceptions (mirrors [Actuators])
 * Every branch is wrapped so NOTHING throws: an unavailable app Context →
 * `success=false, "app context unavailable"`; a missing app
 * ([ActivityNotFoundException]) or any other failure →
 * `success=false, "<name> failed (<ExceptionClass>)"`; an unknown action name →
 * `success=false, "unknown intent action: <name>"`; a missing required arg →
 * `success=false, "<key> required"`.
 *
 * ## Autonomy gate (IA-1) — applied to the only two "sends on your behalf" intents
 * `send_email` and `send_sms` are the sole [HIGH_CONSEQUENCE_INTENTS]. In
 * [AutonomyMode.PERMISSION] this actuator asks [confirm] (the user) BEFORE building
 * /firing them, via the pure [shouldConfirmIntent] + [describeIntent] decision; a
 * decline returns `"user declined"` without launching anything. In
 * [AutonomyMode.YOLO] nothing gates. Every other intent is either benign or
 * finalized by the user inside the launched UI (the dialer's Call button, the
 * calendar editor), so a separate confirm would be redundant over-gating.
 *
 * ## Leak discipline (HARD — shared with [Actuators]/[ConfirmGate])
 * NOTHING sensitive is ever logged or placed into an [ActuatorResult.detail]: not
 * an email body/subject/recipient, sms body, contact phone/email, url, search
 * query, share text, or alarm/event label/title. Logs emit ONLY the action name +
 * coarse ok flag (mirroring [Actuators.logAction]); every success detail string is
 * a FIXED, generic phrase (e.g. "opened maps"). The ONLY place a recipient/number
 * surfaces is inside [describeIntent] (the confirm prompt), which is correct and by
 * design — and only fixed tool-registry names ever reach [describeIntent].
 *
 * @param context seam to the process-wide Application [Context] (prod:
 *   `{ appContext }`); used for `startActivity` / `getSystemService`. Resolved each
 *   call and normalized to `applicationContext`, so the launch outlives any
 *   short-lived caller and needs no accessibility.
 * @param mode reads the current device autonomy posture each time it's needed
 *   (default `{ AutonomyMode.YOLO }` so un-wired call-sites behave as before; the
 *   SAFE PERMISSION default is supplied by the production wiring).
 * @param confirm the user-confirmation seam for the gated `send_*` intents in
 *   Permission mode (prod: [OverlayConfirmUi]; default auto-approve no-op).
 */
class IntentActuator(
    private val context: () -> android.content.Context?,
    private val mode: () -> AutonomyMode = { AutonomyMode.YOLO },
    private val confirm: ConfirmUi = AutoApproveConfirmUi,
) {

    /**
     * Build + fire the intent action [name] with Gemma's JSON [args].
     *
     * Resolves the Application [Context]; on `null` returns
     * `"app context unavailable"`. Dispatches over the 15 known actions;
     * an unknown [name] → `"unknown intent action: <name>"`. EVERY branch is
     * wrapped so nothing throws — a launch failure ([ActivityNotFoundException] or
     * anything else) becomes `"<name> failed (<ExceptionClass>)"`. The two
     * `send_*` actions consult the autonomy gate ([shouldConfirmIntent]) BEFORE
     * firing and abort with `"user declined"` if denied.
     */
    suspend fun perform(name: String, args: JsonObject): ActuatorResult {
        val ctx = context()?.applicationContext ?: return ActuatorResult(false, "app context unavailable")
        return try {
            when (name) {
                // 1. flashlight — NOT an intent; drive the camera torch directly.
                "flashlight_on" -> torch(ctx, on = true)
                "flashlight_off" -> torch(ctx, on = false)

                // 2. create_contact — all fields optional (default "").
                "create_contact" -> {
                    val first = str(args, "first_name") ?: ""
                    val last = str(args, "last_name") ?: ""
                    val phone = str(args, "phone_number") ?: ""
                    val email = str(args, "email") ?: ""
                    val intent = Intent(ContactsContract.Intents.Insert.ACTION).apply {
                        type = ContactsContract.RawContacts.CONTENT_TYPE
                        putExtra(ContactsContract.Intents.Insert.NAME, "$first $last".trim())
                        putExtra(ContactsContract.Intents.Insert.EMAIL, email)
                        putExtra(
                            ContactsContract.Intents.Insert.EMAIL_TYPE,
                            ContactsContract.CommonDataKinds.Email.TYPE_WORK,
                        )
                        putExtra(ContactsContract.Intents.Insert.PHONE, phone)
                        putExtra(
                            ContactsContract.Intents.Insert.PHONE_TYPE,
                            ContactsContract.CommonDataKinds.Phone.TYPE_WORK,
                        )
                    }
                    fire(ctx, name, intent, "contact editor opened")
                }

                // 3. send_email — `to` REQUIRED; subject/body optional. [GATE]
                "send_email" -> {
                    val to = str(args, "to") ?: return ActuatorResult(false, "to required")
                    val subject = str(args, "subject")
                    val body = str(args, "body")
                    // AUTONOMY GATE — ask BEFORE building/firing. primaryArg = recipient
                    // (never the body — describeIntent only ever shows the recipient).
                    if (shouldConfirmIntent(mode(), name)) {
                        if (!confirm.confirm(describeIntent(name, to))) {
                            return ActuatorResult(false, "user declined")
                        }
                    }
                    val intent = Intent(Intent.ACTION_SEND).apply {
                        data = Uri.parse("mailto:")
                        type = "text/plain"
                        putExtra(Intent.EXTRA_EMAIL, arrayOf(to))
                        putExtra(Intent.EXTRA_SUBJECT, subject ?: "")
                        putExtra(Intent.EXTRA_TEXT, body ?: "")
                    }
                    fire(ctx, name, intent, "opened email composer")
                }

                // 4. show_map — query REQUIRED (geoQueryUri form-encodes it).
                "show_map" -> {
                    val query = str(args, "query") ?: return ActuatorResult(false, "query required")
                    val intent = Intent(Intent.ACTION_VIEW).apply {
                        data = Uri.parse(geoQueryUri(query))
                    }
                    fire(ctx, name, intent, "opened maps")
                }

                // 5. open_wifi_settings.
                "open_wifi_settings" ->
                    fire(ctx, name, Intent(Settings.ACTION_WIFI_SETTINGS), "opened wifi settings")

                // 6. create_calendar_event — datetime REQUIRED, title optional.
                "create_calendar_event" -> {
                    val datetime = str(args, "datetime")
                        ?: return ActuatorResult(false, "datetime required")
                    val title = str(args, "title")
                    val begin = calendarMillis(datetime, System.currentTimeMillis())
                    val intent = Intent(Intent.ACTION_INSERT).apply {
                        data = CalendarContract.Events.CONTENT_URI
                        putExtra(CalendarContract.Events.TITLE, title ?: "")
                        putExtra(CalendarContract.EXTRA_EVENT_BEGIN_TIME, begin)
                        putExtra(CalendarContract.EXTRA_EVENT_END_TIME, calendarEndMillis(begin))
                    }
                    fire(ctx, name, intent, "calendar editor opened")
                }

                // 7. open_url — url REQUIRED; REJECT non-web schemes (isWebUrl).
                "open_url" -> {
                    val url = str(args, "url") ?: return ActuatorResult(false, "url required")
                    if (!isWebUrl(url)) return ActuatorResult(false, "invalid url")
                    val intent = Intent(Intent.ACTION_VIEW).apply { data = Uri.parse(url) }
                    fire(ctx, name, intent, "opened link")
                }

                // 8. dial — number REQUIRED (telUri sanitizes to dialer chars).
                "dial" -> {
                    val number = str(args, "number") ?: return ActuatorResult(false, "number required")
                    val intent = Intent(Intent.ACTION_DIAL).apply { data = Uri.parse(telUri(number)) }
                    fire(ctx, name, intent, "opened dialer")
                }

                // 9. send_sms — number REQUIRED, body optional. [GATE]
                "send_sms" -> {
                    val number = str(args, "number") ?: return ActuatorResult(false, "number required")
                    val body = str(args, "body")
                    // AUTONOMY GATE — ask BEFORE building/firing. primaryArg = number
                    // (never the body — describeIntent only ever shows the number).
                    if (shouldConfirmIntent(mode(), name)) {
                        if (!confirm.confirm(describeIntent(name, number))) {
                            return ActuatorResult(false, "user declined")
                        }
                    }
                    val intent = Intent(Intent.ACTION_SENDTO).apply {
                        data = Uri.parse(smsToUri(number))
                        putExtra("sms_body", body ?: "")
                    }
                    fire(ctx, name, intent, "opened messaging")
                }

                // 10. set_alarm — hour & minutes REQUIRED ints; label optional.
                "set_alarm" -> {
                    val hour = intArg(args, "hour") ?: return ActuatorResult(false, "hour required")
                    val minutes = intArg(args, "minutes")
                        ?: return ActuatorResult(false, "minutes required")
                    val label = str(args, "label")
                    val intent = Intent(AlarmClock.ACTION_SET_ALARM).apply {
                        putExtra(AlarmClock.EXTRA_HOUR, clampHour(hour))
                        putExtra(AlarmClock.EXTRA_MINUTES, clampMinutes(minutes))
                        putExtra(AlarmClock.EXTRA_MESSAGE, label ?: "")
                        putExtra(AlarmClock.EXTRA_SKIP_UI, false)
                    }
                    fire(ctx, name, intent, "alarm set")
                }

                // 11. set_timer — seconds REQUIRED int; label optional.
                "set_timer" -> {
                    val seconds = intArg(args, "seconds")
                        ?: return ActuatorResult(false, "seconds required")
                    val label = str(args, "label")
                    val intent = Intent(AlarmClock.ACTION_SET_TIMER).apply {
                        putExtra(AlarmClock.EXTRA_LENGTH, clampTimerSeconds(seconds))
                        putExtra(AlarmClock.EXTRA_MESSAGE, label ?: "")
                        putExtra(AlarmClock.EXTRA_SKIP_UI, false)
                    }
                    fire(ctx, name, intent, "timer set")
                }

                // 12. share_text — text REQUIRED; fires via a chooser (needs NEW_TASK).
                "share_text" -> {
                    val text = str(args, "text") ?: return ActuatorResult(false, "text required")
                    val send = Intent(Intent.ACTION_SEND).apply {
                        type = "text/plain"
                        putExtra(Intent.EXTRA_TEXT, text)
                    }
                    try {
                        ctx.startActivity(
                            Intent.createChooser(send, null).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK),
                        )
                        logFired(name, true)
                        ActuatorResult(true, "share sheet opened")
                    } catch (e: Exception) {
                        logFired(name, false)
                        ActuatorResult(false, "$name failed (${e.javaClass.simpleName})")
                    }
                }

                // 13. open_settings_panel — which optional; settingsPanelAction never null.
                "open_settings_panel" -> {
                    val which = str(args, "which")
                    fire(ctx, name, Intent(settingsPanelAction(which)), "opened settings")
                }

                // 14. take_photo.
                "take_photo" ->
                    fire(ctx, name, Intent(MediaStore.ACTION_IMAGE_CAPTURE), "camera opened")

                // web_search is intentionally NOT an intent action: the on-device model's
                // web search is HEADLESS (routed through the cloud ToolBridge so it never
                // backgrounds the app). See ResidentTools.webSearchSchema / ChatViewModel
                // .buildCloudNativeTools. Do not re-add an ACTION_WEB_SEARCH branch here.
                else -> ActuatorResult(false, "unknown intent action: $name")
            }
        } catch (e: Exception) {
            // Belt-and-suspenders: each branch already never-throws, but guarantee
            // the whole dispatch is exception-free. Class name only — no args.
            logFired(name, false)
            ActuatorResult(false, "$name failed (${e.javaClass.simpleName})")
        }
    }

    // ---- internals --------------------------------------------------------

    /**
     * Toggle the device flashlight via [CameraManager.setTorchMode] (the torch is
     * NOT an intent). Finds the first camera that advertises a flash unit; if none
     * → `"no flash available"`. Wrapped so a camera-service / torch-mode failure
     * becomes a graceful `success=false` rather than throwing.
     */
    private fun torch(ctx: Context, on: Boolean): ActuatorResult {
        val action = if (on) "flashlight_on" else "flashlight_off"
        return try {
            val cm = ctx.getSystemService(Context.CAMERA_SERVICE) as CameraManager
            val id = cm.cameraIdList.firstOrNull {
                cm.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true
            } ?: return ActuatorResult(false, "no flash available")
            cm.setTorchMode(id, on)
            logFired(action, true)
            ActuatorResult(true, if (on) "flashlight on" else "flashlight off")
        } catch (e: Exception) {
            logFired(action, false)
            ActuatorResult(false, "$action failed (${e.javaClass.simpleName})")
        }
    }

    /**
     * Fire a built [intent] via `startActivity` (NEW_TASK, required to launch from a
     * non-Activity [Context] like the Application context). Returns `success=true, okDetail` on
     * launch; an [ActivityNotFoundException] / any error →
     * `success=false, "<name> failed (<ExceptionClass>)"`. [okDetail] MUST be a
     * fixed, NON-sensitive phrase — it is never derived from the args.
     */
    private fun fire(
        ctx: Context,
        name: String,
        intent: Intent,
        okDetail: String,
    ): ActuatorResult = try {
        ctx.startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        logFired(name, true)
        ActuatorResult(true, okDetail)
    } catch (e: ActivityNotFoundException) {
        logFired(name, false)
        ActuatorResult(false, "$name failed (${e.javaClass.simpleName})")
    } catch (e: Exception) {
        logFired(name, false)
        ActuatorResult(false, "$name failed (${e.javaClass.simpleName})")
    }

    /**
     * Log ONLY the action name + coarse ok flag (mirrors [Actuators.logAction]).
     * NEVER logs any argument — no recipient, number, url, query, body, or label.
     */
    private fun logFired(name: String, ok: Boolean) {
        Log.i(TAG, "intent $name ok=$ok")
    }

    /**
     * PRIVATE: a non-blank String arg, or null. [args]`[key]` must be a JSON
     * primitive whose content is non-blank.
     */
    private fun str(args: JsonObject, key: String): String? =
        (args[key] as? JsonPrimitive)?.contentOrNull?.takeIf { it.isNotBlank() }

    /**
     * PRIVATE: a tolerant Int arg, or null. Mirrors [parseNodeId]: Gemma emits JSON
     * numbers with a decimal point (e.g. `8.0`), so accept an int (`8`), a float
     * (`8.0` → 8 via [doubleOrNull]), or a string form (`"8"` / `"8.0"`). Returns
     * null only when truly absent / non-numeric.
     */
    private fun intArg(args: JsonObject, key: String): Int? {
        val prim = args[key]?.jsonPrimitive ?: return null
        prim.intOrNull?.let { return it }
        prim.doubleOrNull?.let { return it.toInt() }
        return prim.contentOrNull?.toDoubleOrNull()?.toInt()
    }

    companion object {
        private const val TAG = "IntentActuator"

        /**
         * Production factory: actuates through the process-wide Application
         * [Context] — so the benign OS intents fire even with the accessibility
         * service DISABLED (Gallery parity). [appContext] is normalized to its
         * `applicationContext` per call.
         *
         * @param appContext any [Context]; its [Context.getApplicationContext] is
         *   used as the long-lived launch Context.
         * @param mode reads the device autonomy posture for the `send_*` gate (prod
         *   wiring supplies a SharedPref-backed read defaulting to
         *   [AutonomyMode.PERMISSION] — the SAFE default). Defaults to YOLO here
         *   ONLY so an un-wired call keeps pre-gate behavior.
         * @param confirm the user-confirmation seam (prod: [OverlayConfirmUi]).
         */
        fun fromAppContext(
            appContext: android.content.Context,
            mode: () -> AutonomyMode = { AutonomyMode.YOLO },
            confirm: ConfirmUi = AutoApproveConfirmUi,
        ): IntentActuator = IntentActuator({ appContext.applicationContext }, mode, confirm)
    }
}
