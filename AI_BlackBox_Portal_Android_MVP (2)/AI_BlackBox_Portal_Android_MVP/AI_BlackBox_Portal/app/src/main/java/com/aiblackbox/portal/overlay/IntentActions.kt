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
 * PURE: map a settings-panel name [which] to its `Settings.ACTION_*` activity
 * action String. NEVER returns null.
 *
 * Matching is case-insensitive + trimmed. An unknown, blank, or null [which]
 * falls back to the top-level [Settings.ACTION_SETTINGS] — always landing the
 * user *somewhere* useful rather than failing. (These are inlined String
 * constants, so this resolves on the host JVM with no `android.jar`.)
 *
 * Mapping:
 *  - `wifi` → [Settings.ACTION_WIFI_SETTINGS]
 *  - `bluetooth` → [Settings.ACTION_BLUETOOTH_SETTINGS]
 *  - `location` → [Settings.ACTION_LOCATION_SOURCE_SETTINGS]
 *  - `sound` → [Settings.ACTION_SOUND_SETTINGS]
 *  - `display` → [Settings.ACTION_DISPLAY_SETTINGS]
 *  - `battery` → [Settings.ACTION_BATTERY_SAVER_SETTINGS]
 *  - `nfc` → [Settings.ACTION_NFC_SETTINGS]
 *  - `airplane` → [Settings.ACTION_AIRPLANE_MODE_SETTINGS]
 *  - `data` / `cellular` → [Settings.ACTION_DATA_ROAMING_SETTINGS]
 *  - `storage` → [Settings.ACTION_INTERNAL_STORAGE_SETTINGS]
 *  - `apps` → [Settings.ACTION_APPLICATION_SETTINGS]
 *  - anything else (incl. null/blank) → [Settings.ACTION_SETTINGS]
 */
fun settingsPanelAction(which: String?): String = when (which?.trim()?.lowercase()) {
    "wifi" -> Settings.ACTION_WIFI_SETTINGS
    "bluetooth" -> Settings.ACTION_BLUETOOTH_SETTINGS
    "location" -> Settings.ACTION_LOCATION_SOURCE_SETTINGS
    "sound" -> Settings.ACTION_SOUND_SETTINGS
    "display" -> Settings.ACTION_DISPLAY_SETTINGS
    "battery" -> Settings.ACTION_BATTERY_SAVER_SETTINGS
    "nfc" -> Settings.ACTION_NFC_SETTINGS
    "airplane" -> Settings.ACTION_AIRPLANE_MODE_SETTINGS
    "data", "cellular" -> Settings.ACTION_DATA_ROAMING_SETTINGS
    "storage" -> Settings.ACTION_INTERNAL_STORAGE_SETTINGS
    "apps" -> Settings.ACTION_APPLICATION_SETTINGS
    else -> Settings.ACTION_SETTINGS
}

/** PURE: clamp an alarm/event [hour] into the valid 0..23 range. */
fun clampHour(hour: Int): Int = hour.coerceIn(0, 23)

/** PURE: clamp an alarm/event [minutes] into the valid 0..59 range. */
fun clampMinutes(minutes: Int): Int = minutes.coerceIn(0, 59)

/** PURE: clamp a timer length [seconds] into 1s..24h (1..86_400). */
fun clampTimerSeconds(seconds: Int): Int = seconds.coerceIn(1, 86_400)
