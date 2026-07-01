package com.aiblackbox.portal.overlay

import android.provider.Settings
import java.net.URLEncoder
import java.nio.charset.StandardCharsets
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * PURE intent-argument builders for the on-device "intent-based phone actions"
 * feature (Task IA-1).
 *
 * An *intent action* is a benign, well-bounded request the on-device Gemma agent
 * can satisfy by launching a stock Android intent — show a place on a map, dial a
 * number, draft an SMS/email, open a settings panel, set a timer/alarm, run a web
 * search. The Android `Intent` construction itself (and every framework call)
 * lives in the IntentActuator (Task IA-2); THIS file holds only the pure
 * String/Long transforms that prepare each intent's arguments, so the entire
 * URI / sanitization / clamp surface is exhaustively host-JVM unit-testable.
 *
 * ## Why "pure builders + framework actuator"
 * The risky part of each intent is the *argument* — a `tel:` URI that smuggled
 * dialer-control characters, an `open_url` that launched a non-web scheme, a
 * `geo:` query that broke out of its own query string. Each of those is a pure
 * transform decided here, with zero Android *method* dependency, so it can be
 * pinned with exhaustive tests. (We MAY reference Android `static final String`
 * constants like [Settings.ACTION_WIFI_SETTINGS]: the Kotlin/Java compiler inlines
 * a compile-time constant to a real String literal, so host unit tests resolve
 * them without ever loading `android.jar`.)
 *
 * ## Verbatim parity with Google's AI Edge Gallery
 * [geoQueryUri] and the calendar millis helpers mirror the Gallery sample's
 * `showLocationOnMap` / `createCalendarEvent` exactly, so our intents behave
 * identically to a reference Gemma function-calling host.
 */

/**
 * PURE: the `geo:` map URI for a free-text place [query].
 *
 * Returns `geo:0,0?q=<url-encoded query>` — the `0,0` anchor with a `q=` label is
 * the canonical "search for this text on a map" form (the map app geocodes the
 * label, ignoring the 0,0 coordinates). The query is form-encoded via
 * [URLEncoder] (spaces become `+`, reserved chars like `&` are percent-encoded),
 * so it can never break out of the query string. Matches Google's Gallery
 * `showLocationOnMap` verbatim.
 */
fun geoQueryUri(query: String): String =
    "geo:0,0?q=" + URLEncoder.encode(query, StandardCharsets.UTF_8.toString())

/**
 * PURE: the `tel:` dialer URI for a phone [number].
 *
 * SANITIZATION (SAFETY): keeps ONLY characters a dialer legitimately uses —
 * digits and `+ * #` — and strips everything else (letters, parens, spaces,
 * dashes, and any control/separator characters). This guarantees the resulting
 * URI cannot carry extra path/query segments or smuggle a second action; the
 * dialer merely receives a clean number to pre-fill. Vanity letters are stripped
 * rather than converted (we do not invent digits).
 */
fun telUri(number: String): String =
    "tel:" + number.filter { it.isDigit() || it in "+*#" }

/**
 * PURE: the `smsto:` URI for drafting a text to [number].
 *
 * Uses the identical dialer-char sanitization as [telUri] (digits + `+ * #`
 * only), with the `smsto:` scheme so the messaging app opens a draft to that
 * recipient. The message BODY is never part of this URI — it is supplied
 * separately as an intent extra by the actuator (Task IA-2) and never flows
 * through this pure helper.
 */
fun smsToUri(number: String): String =
    "smsto:" + number.filter { it.isDigit() || it in "+*#" }

/**
 * PURE: is [url] a real web URL (http/https with a non-empty host)?
 *
 * Used to REJECT a non-web `open_url` argument BEFORE the actuator builds a
 * VIEW intent: a Gemma-produced `open_url` must only ever launch the browser, not
 * a `tel:` / `geo:` / `file:` / arbitrary-scheme intent (which could reach a
 * different, possibly sensitive, app). Returns true iff the trimmed [url] matches
 * `^https?://<non-empty, whitespace-free host>...` with a case-insensitive
 * scheme. `http://` with no host, `ftp://…`, bare words, and blanks all fail.
 */
fun isWebUrl(url: String): Boolean =
    Regex("^https?://[^\\s/]+.*", RegexOption.IGNORE_CASE).matches(url.trim())

/**
 * PURE: epoch-millis for a calendar event start, parsed from an ISO [datetime].
 *
 * Parses [datetime] as a [LocalDateTime] (ISO `YYYY-MM-DDTHH:MM:SS`), interprets
 * it in the device's [ZoneId.systemDefault] zone, and returns the epoch
 * millisecond instant. On ANY exception (malformed/empty/out-of-range string) it
 * returns the supplied [nowMs] fallback, so the result is always deterministic
 * (and unit tests can pin the fallback exactly). Mirrors Google's Gallery
 * `createCalendarEvent`.
 */
fun calendarMillis(datetime: String, nowMs: Long): Long =
    try {
        LocalDateTime.parse(datetime)
            .atZone(ZoneId.systemDefault())
            .toInstant()
            .toEpochMilli()
    } catch (e: Exception) {
        nowMs
    }

/**
 * PURE: the calendar event END millis — exactly one hour after [beginMs].
 *
 * A fixed 1-hour default duration, matching Google's Gallery `createCalendarEvent`
 * (the user can adjust the duration in the calendar editor that the intent opens).
 */
fun calendarEndMillis(beginMs: Long): Long = beginMs + 3_600_000L

/**
 * PURE: the COMPREHENSIVE settings-panel catalog — a normalized key → its
 * `Settings.ACTION_*` activity-action String (decision 9, `open_settings(panel)`).
 *
 * This is the single source of truth for the full "open any settings panel" surface
 * the frontier model reaches via the fast intent path. Keys are the lower-cased,
 * trimmed names the model supplies; values are Android's inlined
 * `public static final String` action constants (so this whole map resolves on the
 * host JVM with no `android.jar` — the same trick the whole [IntentActions] file
 * relies on). Aliases (e.g. `cellular`/`data`/`data_roaming`) map to the same
 * action so the model doesn't have to guess the exact spelling.
 *
 * HONESTY: only panels that resolve WITHOUT extra data (no `package:` URI / no
 * required extras) are listed here, so `open_settings(panel)` — which takes only a
 * panel key — always lands on a real screen. `hotspot`/`tethering` have no public
 * dedicated action, so they map to the nearest public umbrella
 * ([Settings.ACTION_WIRELESS_SETTINGS]); a SPECIFIC app's detail / write-settings /
 * app-notification panel needs a `package:` URI and is reached through the guarded
 * generic `send_intent` escape-hatch instead, not here.
 *
 * Ordering is stable (LinkedHashMap) so [settingsPanelKeys] reads sensibly in the
 * "unknown panel" error the actuator returns.
 */
val SETTINGS_PANELS: Map<String, String> = linkedMapOf(
    // Connectivity / radios
    "wifi" to Settings.ACTION_WIFI_SETTINGS,
    "bluetooth" to Settings.ACTION_BLUETOOTH_SETTINGS,
    "wireless" to Settings.ACTION_WIRELESS_SETTINGS,
    "hotspot" to Settings.ACTION_WIRELESS_SETTINGS, // no public hotspot action → nearest umbrella
    "tethering" to Settings.ACTION_WIRELESS_SETTINGS,
    "airplane" to Settings.ACTION_AIRPLANE_MODE_SETTINGS,
    "nfc" to Settings.ACTION_NFC_SETTINGS,
    "nfc_sharing" to Settings.ACTION_NFCSHARING_SETTINGS,
    "data" to Settings.ACTION_DATA_ROAMING_SETTINGS, // back-compat alias (roaming)
    "cellular" to Settings.ACTION_DATA_ROAMING_SETTINGS,
    "data_roaming" to Settings.ACTION_DATA_ROAMING_SETTINGS,
    "data_usage" to Settings.ACTION_DATA_USAGE_SETTINGS,
    "apn" to Settings.ACTION_APN_SETTINGS,
    "cast" to Settings.ACTION_CAST_SETTINGS,
    // Display / sound
    "display" to Settings.ACTION_DISPLAY_SETTINGS,
    "sound" to Settings.ACTION_SOUND_SETTINGS,
    "screensaver" to Settings.ACTION_DREAM_SETTINGS,
    "captioning" to Settings.ACTION_CAPTIONING_SETTINGS,
    // Location / privacy / security
    "location" to Settings.ACTION_LOCATION_SOURCE_SETTINGS,
    "security" to Settings.ACTION_SECURITY_SETTINGS,
    "privacy" to Settings.ACTION_PRIVACY_SETTINGS,
    "accessibility" to Settings.ACTION_ACCESSIBILITY_SETTINGS,
    "usage_access" to Settings.ACTION_USAGE_ACCESS_SETTINGS,
    "notifications" to Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS,
    "notification_listener" to Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS,
    "overlay" to Settings.ACTION_MANAGE_OVERLAY_PERMISSION,
    "unknown_sources" to Settings.ACTION_MANAGE_UNKNOWN_APP_SOURCES,
    // Power / storage
    "battery" to Settings.ACTION_BATTERY_SAVER_SETTINGS,
    "battery_optimization" to Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS,
    "storage" to Settings.ACTION_INTERNAL_STORAGE_SETTINGS,
    "memory_card" to Settings.ACTION_MEMORY_CARD_SETTINGS,
    // Date / language / input
    "date" to Settings.ACTION_DATE_SETTINGS,
    "time" to Settings.ACTION_DATE_SETTINGS,
    "language" to Settings.ACTION_LOCALE_SETTINGS,
    "locale" to Settings.ACTION_LOCALE_SETTINGS,
    "input_method" to Settings.ACTION_INPUT_METHOD_SETTINGS,
    "keyboard" to Settings.ACTION_HARD_KEYBOARD_SETTINGS,
    "dictionary" to Settings.ACTION_USER_DICTIONARY_SETTINGS,
    "voice_input" to Settings.ACTION_VOICE_INPUT_SETTINGS,
    // Accounts / apps / device
    "sync" to Settings.ACTION_SYNC_SETTINGS,
    "add_account" to Settings.ACTION_ADD_ACCOUNT,
    "apps" to Settings.ACTION_APPLICATION_SETTINGS,
    "manage_apps" to Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS,
    "all_apps" to Settings.ACTION_MANAGE_ALL_APPLICATIONS_SETTINGS,
    "default_apps" to Settings.ACTION_MANAGE_DEFAULT_APPS_SETTINGS,
    "developer" to Settings.ACTION_APPLICATION_DEVELOPMENT_SETTINGS,
    "device_info" to Settings.ACTION_DEVICE_INFO_SETTINGS,
    "about" to Settings.ACTION_DEVICE_INFO_SETTINGS,
    "home" to Settings.ACTION_HOME_SETTINGS,
    // Top-level
    "settings" to Settings.ACTION_SETTINGS,
)

/**
 * PURE: the sorted list of valid [SETTINGS_PANELS] keys — surfaced in the graceful
 * "unknown settings panel" error the actuator returns so the model can self-correct.
 * All keys are fixed constants (never user data), so listing them leaks nothing.
 */
fun settingsPanelKeys(): List<String> = SETTINGS_PANELS.keys.sorted()

/**
 * PURE: map a settings-panel name [which] to its `Settings.ACTION_*` action, or
 * `null` if [which] is unknown/blank/null. Case-insensitive + trimmed lookup into
 * [SETTINGS_PANELS]. Used by the comprehensive `open_settings(panel)` intent, which
 * returns a graceful error (listing [settingsPanelKeys]) on `null` rather than
 * silently landing somewhere.
 */
fun settingsPanelActionOrNull(which: String?): String? =
    which?.trim()?.lowercase()?.let { SETTINGS_PANELS[it] }

/**
 * PURE: map a settings-panel name [which] to its `Settings.ACTION_*` activity
 * action String. NEVER returns null — an unknown/blank/null [which] falls back to
 * the top-level [Settings.ACTION_SETTINGS].
 *
 * This is the LENIENT resolver behind the legacy `open_settings_panel` intent
 * (always lands the user *somewhere* useful). The comprehensive `open_settings`
 * intent uses the STRICT [settingsPanelActionOrNull] instead (unknown → error).
 * Both read the single [SETTINGS_PANELS] catalog, so they never drift.
 */
fun settingsPanelAction(which: String?): String =
    settingsPanelActionOrNull(which) ?: Settings.ACTION_SETTINGS

/** PURE: clamp an alarm/event [hour] into the valid 0..23 range. */
fun clampHour(hour: Int): Int = hour.coerceIn(0, 23)

/** PURE: clamp an alarm/event [minutes] into the valid 0..59 range. */
fun clampMinutes(minutes: Int): Int = minutes.coerceIn(0, 59)

/** PURE: clamp a timer length [seconds] into 1s..24h (1..86_400). */
fun clampTimerSeconds(seconds: Int): Int = seconds.coerceIn(1, 86_400)

// =============================================================================
// Decision-9 additions: broad deep-link open_url · navigation · the guarded
// generic send_intent escape-hatch (all PURE / host-JVM-testable)
// =============================================================================

/**
 * URI schemes that must NEVER be launched via a generic ACTION_VIEW / `send_intent`
 * on the user's behalf — the file-exposure / implicit-grant / intent-smuggling
 * vectors:
 *  - `file` — `FileUriExposedException` risk + reads a raw filesystem path.
 *  - `content` — could smuggle an implicit URI-permission GRANT to a private provider.
 *  - `android_resource` — reaches another app's private resources.
 *  - `intent` — the classic deep-link SMUGGLING vector: `intent://…#Intent;…;end`
 *    is parsed by the target into a FULL intent (arbitrary action/extras/flags),
 *    exactly the "dangerous/implicit-grant flags" bypass we must reject.
 *  - `javascript` / `data` — script / inline-payload execution surfaces.
 *
 * Everything else (http/https AND app deep-link schemes like `tel`/`geo`/`mailto`/
 * `sms`/`spotify`/`myapp`/…) is a legitimate, user-visible VIEW target.
 */
val UNSAFE_URI_SCHEMES: Set<String> =
    setOf("file", "content", "android_resource", "intent", "javascript", "data")

/** PURE: the lower-cased scheme of [uri] (text before the first `:`), or "" if none. */
private fun schemeOf(uri: String): String =
    uri.trim().substringBefore(":", "").lowercase()

/**
 * PURE: is [uri] safe to hand to a generic ACTION_VIEW (the broadened
 * `open_url(uri)` — decision 9: "any http/https OR app deep-link URI, one
 * primitive covering all deep links")?
 *
 * Accepts any URI that HAS a scheme, a non-blank scheme-specific part, and a scheme
 * NOT in [UNSAFE_URI_SCHEMES]. So `https://…`, `http://…`, `tel:…`, `geo:…`,
 * `mailto:…`, `sms:…`, and arbitrary app deep links (`spotify:…`, `myapp://…`)
 * all pass, while `file:`/`content:`/`intent:`/`javascript:`/`data:` and a
 * scheme-less bare string are REJECTED. Broader than the web-only [isWebUrl]
 * (which is retained for callers that must stay web-only).
 */
fun isSafeViewUri(uri: String): Boolean {
    val u = uri.trim()
    if (u.isEmpty()) return false
    val scheme = schemeOf(u)
    if (scheme.isEmpty()) return false
    if (scheme in UNSAFE_URI_SCHEMES) return false
    return u.substringAfter(":", "").isNotBlank()
}

/**
 * PURE: the `google.navigation:` turn-by-turn URI for a free-text [destination].
 *
 * `google.navigation:q=<url-encoded destination>` is the canonical "start
 * turn-by-turn navigation to this place" deep link (Google Maps consumes it). The
 * destination is form-encoded via [URLEncoder] exactly like [geoQueryUri], so it
 * can never break out of the query string.
 */
fun navigationUri(destination: String): String =
    "google.navigation:q=" + URLEncoder.encode(destination, StandardCharsets.UTF_8.toString())

/**
 * Intent ACTIONS that must NEVER be reachable through the guarded generic
 * `send_intent` escape-hatch — the ones that fire a HIGH-CONSEQUENCE, often
 * SILENT, side effect with no user-facing confirmation UI of their own:
 *  - `CALL` / `CALL_PRIVILEGED` / `CALL_EMERGENCY` — place a call with no dialer
 *    confirm (would also need `CALL_PHONE`); the safe path is `dial` (pre-fill only).
 *  - `(UN)INSTALL_PACKAGE` / `REQUEST_INSTALL_PACKAGES` / `DELETE` — install/remove
 *    software or delete data.
 *  - `MASTER_CLEAR` / `FACTORY_RESET` — wipe the device.
 *  - `REBOOT` / `*SHUTDOWN` — power state.
 *  - `REQUEST_PERMISSIONS` — silently escalate granted permissions.
 *
 * Compared case-insensitively (a model may vary the casing of the action string).
 */
val DANGEROUS_SEND_INTENT_ACTIONS: Set<String> = setOf(
    "android.intent.action.call",
    "android.intent.action.call_privileged",
    "android.intent.action.call_emergency",
    "android.intent.action.install_package",
    "android.intent.action.uninstall_package",
    "android.intent.action.request_install_packages",
    "android.intent.action.delete",
    "android.content.pm.action.request_permissions",
    "android.intent.action.master_clear",
    "android.intent.action.factory_reset",
    "android.intent.action.reboot",
    "android.intent.action.action_request_shutdown",
    "android.intent.action.action_shutdown",
    "com.android.internal.intent.action.request_shutdown",
)

/**
 * PURE: the SAFETY ENVELOPE for the guarded generic `send_intent(action, uri?,
 * extras?, mime?, package?)` escape-hatch. Returns a graceful, NON-leaking
 * rejection reason (a fixed phrase — never any argument content), or `null` when
 * the request is allowed to proceed to the confirm-gate + launch.
 *
 * Rules (all decided here so they are exhaustively unit-testable):
 *  1. [action] must be present and non-blank.
 *  2. [action] must NOT be in [DANGEROUS_SEND_INTENT_ACTIONS] (silent
 *     call/install/delete/wipe/power/permission-escalation).
 *  3. If [uri] is present it must carry a scheme, and that scheme must NOT be in
 *     [UNSAFE_URI_SCHEMES] (file/content/android_resource/intent/javascript/data —
 *     the file-exposure / implicit-grant / intent-smuggling vectors).
 *
 * The actuator additionally (a) routes `send_intent` through the high-consequence
 * confirm-gate ([shouldConfirmIntent]) and (b) sets ONLY `FLAG_ACTIVITY_NEW_TASK`
 * (never a `FLAG_GRANT_*_URI_PERMISSION` / new-document flag) and coerces extras to
 * plain Strings — so no dangerous flag can be smuggled in through this function's
 * callers either. Credential handoff is never bypassed: a fire-and-forget intent
 * cannot type into a field, and the safe actions here never carry secrets.
 */
fun sendIntentRejectionReason(
    action: String?,
    uri: String?,
    @Suppress("UNUSED_PARAMETER") mime: String?,
    @Suppress("UNUSED_PARAMETER") pkg: String?,
): String? {
    val act = action?.trim().orEmpty()
    if (act.isEmpty()) return "action required"
    if (act.lowercase() in DANGEROUS_SEND_INTENT_ACTIONS) return "action not permitted via send_intent"
    if (uri != null) {
        val scheme = schemeOf(uri)
        if (scheme.isEmpty()) return "uri scheme required"
        if (scheme in UNSAFE_URI_SCHEMES) return "unsafe uri scheme"
    }
    return null
}
