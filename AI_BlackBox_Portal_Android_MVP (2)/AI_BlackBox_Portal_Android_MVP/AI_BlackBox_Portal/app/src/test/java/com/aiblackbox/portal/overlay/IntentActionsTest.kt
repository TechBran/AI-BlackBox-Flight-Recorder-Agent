package com.aiblackbox.portal.overlay

import android.provider.Settings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Unit tests for the PURE intent-argument builders (Task IA-1).
 *
 * Every function in [IntentActions] is a pure String/Long transform with no
 * Android *method* call (only inlined `Settings.ACTION_*` String constants), so
 * the entire URI/sanitization/clamp surface is exhaustively testable on the host
 * JVM. These are the SAFETY-relevant transforms — a `tel:` URI that smuggled
 * shell/dialer-control characters, or an `open_url` that launched a non-web
 * scheme, would be a real bug — so each is pinned here.
 */
class IntentActionsTest {

    // ---- geoQueryUri -------------------------------------------------------

    @Test
    fun `geoQueryUri encodes spaces as plus`() {
        // URLEncoder renders a space as '+' (form encoding) — assert the ACTUAL output.
        assertEquals("geo:0,0?q=coffee+shop", geoQueryUri("coffee shop"))
    }

    @Test
    fun `geoQueryUri encodes ampersand and other reserved chars`() {
        // '&' must be percent-encoded so it can't break out of the query.
        assertEquals("geo:0,0?q=tea+%26+coffee", geoQueryUri("tea & coffee"))
    }

    @Test
    fun `geoQueryUri keeps a plain single-word query intact`() {
        assertEquals("geo:0,0?q=Starbucks", geoQueryUri("Starbucks"))
    }

    // ---- telUri ------------------------------------------------------------

    @Test
    fun `telUri strips letters parens and spaces but keeps digits`() {
        assertEquals("tel:18005551234", telUri("1 (800) 555-1234"))
    }

    @Test
    fun `telUri keeps the leading plus for international numbers`() {
        assertEquals("tel:+447911123456", telUri("+44 7911 123456"))
    }

    @Test
    fun `telUri keeps dialer control chars star and hash`() {
        assertEquals("tel:*67#123", telUri("*67#123"))
    }

    @Test
    fun `telUri strips alphabetic vanity letters`() {
        // Letters are NOT auto-converted; they are stripped (dialer chars only).
        assertEquals("tel:1800", telUri("1-800-FLOWERS".take(6)))
    }

    // ---- smsToUri ----------------------------------------------------------

    @Test
    fun `smsToUri sanitizes the same way but with the smsto prefix`() {
        assertEquals("smsto:18005551234", smsToUri("1 (800) 555-1234"))
    }

    @Test
    fun `smsToUri keeps the leading plus`() {
        assertEquals("smsto:+447911123456", smsToUri("+44 7911 123456"))
    }

    // ---- isWebUrl ----------------------------------------------------------

    @Test
    fun `isWebUrl accepts http and https with a host`() {
        assertTrue(isWebUrl("http://example.com"))
        assertTrue(isWebUrl("https://example.com/path?q=1"))
        assertTrue(isWebUrl("HTTPS://Example.COM")) // scheme is case-insensitive
        assertTrue(isWebUrl("  https://example.com  ")) // trimmed
    }

    @Test
    fun `isWebUrl rejects non-web schemes and junk`() {
        assertFalse(isWebUrl("geo:0,0?q=x"))
        assertFalse(isWebUrl("tel:18005551234"))
        assertFalse(isWebUrl("ftp://example.com"))
        assertFalse(isWebUrl("notaurl"))
        assertFalse(isWebUrl(""))
        assertFalse(isWebUrl("   "))
        assertFalse(isWebUrl("http://")) // no host
        assertFalse(isWebUrl("https:// example.com")) // whitespace in host
    }

    // ---- calendarMillis ----------------------------------------------------

    @Test
    fun `calendarMillis parses a valid ISO datetime`() {
        val datetime = "2026-06-16T14:30:00"
        // Compute the expectation with the SAME java.time call the impl uses.
        val expected = LocalDateTime.parse(datetime)
            .atZone(ZoneId.systemDefault())
            .toInstant()
            .toEpochMilli()
        val now = 999_999L
        val result = calendarMillis(datetime, now)
        assertEquals(expected, result)
        assertFalse("a valid parse must NOT fall back to nowMs", result == now)
    }

    @Test
    fun `calendarMillis returns the exact nowMs fallback on garbage`() {
        val now = 1_700_000_000_000L
        assertEquals(now, calendarMillis("garbage", now))
        assertEquals(now, calendarMillis("", now))
        assertEquals(now, calendarMillis("2026-13-99T99:99:99", now))
    }

    // ---- calendarEndMillis -------------------------------------------------

    @Test
    fun `calendarEndMillis is begin plus one hour`() {
        assertEquals(3_600_000L, calendarEndMillis(0L))
        assertEquals(1_000L + 3_600_000L, calendarEndMillis(1_000L))
    }

    // ---- settingsPanelAction -----------------------------------------------

    @Test
    fun `settingsPanelAction maps each known key to its Settings constant`() {
        assertEquals(Settings.ACTION_WIFI_SETTINGS, settingsPanelAction("wifi"))
        assertEquals(Settings.ACTION_BLUETOOTH_SETTINGS, settingsPanelAction("bluetooth"))
        assertEquals(Settings.ACTION_LOCATION_SOURCE_SETTINGS, settingsPanelAction("location"))
        assertEquals(Settings.ACTION_SOUND_SETTINGS, settingsPanelAction("sound"))
        assertEquals(Settings.ACTION_DISPLAY_SETTINGS, settingsPanelAction("display"))
        assertEquals(Settings.ACTION_BATTERY_SAVER_SETTINGS, settingsPanelAction("battery"))
        assertEquals(Settings.ACTION_NFC_SETTINGS, settingsPanelAction("nfc"))
        assertEquals(Settings.ACTION_AIRPLANE_MODE_SETTINGS, settingsPanelAction("airplane"))
        assertEquals(Settings.ACTION_DATA_ROAMING_SETTINGS, settingsPanelAction("data"))
        assertEquals(Settings.ACTION_DATA_ROAMING_SETTINGS, settingsPanelAction("cellular"))
        assertEquals(Settings.ACTION_INTERNAL_STORAGE_SETTINGS, settingsPanelAction("storage"))
        assertEquals(Settings.ACTION_APPLICATION_SETTINGS, settingsPanelAction("apps"))
    }

    @Test
    fun `settingsPanelAction is case-insensitive and trimmed`() {
        assertEquals(Settings.ACTION_WIFI_SETTINGS, settingsPanelAction("  WiFi  "))
        assertEquals(Settings.ACTION_BLUETOOTH_SETTINGS, settingsPanelAction("BLUETOOTH"))
    }

    @Test
    fun `settingsPanelAction falls back to ACTION_SETTINGS for unknown blank and null`() {
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction("teleport"))
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction(""))
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction("   "))
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction(null))
    }

    // ---- clamp helpers -----------------------------------------------------

    @Test
    fun `clampHour coerces into 0 to 23`() {
        assertEquals(0, clampHour(0))
        assertEquals(23, clampHour(23))
        assertEquals(0, clampHour(-5))
        assertEquals(23, clampHour(99))
        assertEquals(13, clampHour(13))
    }

    @Test
    fun `clampMinutes coerces into 0 to 59`() {
        assertEquals(0, clampMinutes(0))
        assertEquals(59, clampMinutes(59))
        assertEquals(0, clampMinutes(-1))
        assertEquals(59, clampMinutes(60))
        assertEquals(30, clampMinutes(30))
    }

    @Test
    fun `clampTimerSeconds coerces into 1 to 86400`() {
        assertEquals(1, clampTimerSeconds(1))
        assertEquals(86_400, clampTimerSeconds(86_400))
        assertEquals(1, clampTimerSeconds(0))
        assertEquals(1, clampTimerSeconds(-100))
        assertEquals(86_400, clampTimerSeconds(100_000))
        assertEquals(600, clampTimerSeconds(600))
    }

    // =====================================================================
    // Decision-9: comprehensive open_settings catalog
    // =====================================================================

    @Test
    fun `SETTINGS_PANELS maps every original key to the same Settings constant (back-compat)`() {
        // The comprehensive catalog is a SUPERSET of the original open_settings_panel
        // keys — every original mapping must still hold (mirrors settingsPanelAction).
        assertEquals(Settings.ACTION_WIFI_SETTINGS, SETTINGS_PANELS["wifi"])
        assertEquals(Settings.ACTION_BLUETOOTH_SETTINGS, SETTINGS_PANELS["bluetooth"])
        assertEquals(Settings.ACTION_LOCATION_SOURCE_SETTINGS, SETTINGS_PANELS["location"])
        assertEquals(Settings.ACTION_SOUND_SETTINGS, SETTINGS_PANELS["sound"])
        assertEquals(Settings.ACTION_DISPLAY_SETTINGS, SETTINGS_PANELS["display"])
        assertEquals(Settings.ACTION_BATTERY_SAVER_SETTINGS, SETTINGS_PANELS["battery"])
        assertEquals(Settings.ACTION_NFC_SETTINGS, SETTINGS_PANELS["nfc"])
        assertEquals(Settings.ACTION_AIRPLANE_MODE_SETTINGS, SETTINGS_PANELS["airplane"])
        assertEquals(Settings.ACTION_DATA_ROAMING_SETTINGS, SETTINGS_PANELS["data"])
        assertEquals(Settings.ACTION_DATA_ROAMING_SETTINGS, SETTINGS_PANELS["cellular"])
        assertEquals(Settings.ACTION_INTERNAL_STORAGE_SETTINGS, SETTINGS_PANELS["storage"])
        assertEquals(Settings.ACTION_APPLICATION_SETTINGS, SETTINGS_PANELS["apps"])
    }

    @Test
    fun `SETTINGS_PANELS covers the decision-9 catalog beyond the original dozen`() {
        assertEquals(Settings.ACTION_ACCESSIBILITY_SETTINGS, SETTINGS_PANELS["accessibility"])
        assertEquals(Settings.ACTION_SECURITY_SETTINGS, SETTINGS_PANELS["security"])
        assertEquals(Settings.ACTION_DATE_SETTINGS, SETTINGS_PANELS["date"])
        assertEquals(Settings.ACTION_LOCALE_SETTINGS, SETTINGS_PANELS["language"])
        assertEquals(Settings.ACTION_WIRELESS_SETTINGS, SETTINGS_PANELS["wireless"])
        assertEquals(Settings.ACTION_DATA_USAGE_SETTINGS, SETTINGS_PANELS["data_usage"])
        assertEquals(Settings.ACTION_MANAGE_APPLICATIONS_SETTINGS, SETTINGS_PANELS["manage_apps"])
        // hotspot/tethering honestly map to the nearest public umbrella (wireless).
        assertEquals(Settings.ACTION_WIRELESS_SETTINGS, SETTINGS_PANELS["hotspot"])
        // The catalog is comprehensive — well beyond the original dozen.
        assertTrue("catalog should have many panels", SETTINGS_PANELS.size >= 30)
    }

    @Test
    fun `settingsPanelActionOrNull maps a known key and returns null for unknown`() {
        assertEquals(Settings.ACTION_WIFI_SETTINGS, settingsPanelActionOrNull("wifi"))
        assertEquals(Settings.ACTION_WIFI_SETTINGS, settingsPanelActionOrNull("  WiFi  ")) // case + trim
        assertNull(settingsPanelActionOrNull("teleport"))
        assertNull(settingsPanelActionOrNull(""))
        assertNull(settingsPanelActionOrNull("   "))
        assertNull(settingsPanelActionOrNull(null))
    }

    @Test
    fun `settingsPanelKeys is sorted, non-empty, and every key resolves`() {
        val keys = settingsPanelKeys()
        assertTrue("keys not empty", keys.isNotEmpty())
        assertEquals("keys are sorted", keys.sorted(), keys)
        for (k in keys) assertNotNull("key '$k' must resolve", settingsPanelActionOrNull(k))
    }

    @Test
    fun `settingsPanelAction stays lenient (unknown falls back to ACTION_SETTINGS)`() {
        // The legacy open_settings_panel path must keep its never-null fallback.
        assertEquals(Settings.ACTION_WIFI_SETTINGS, settingsPanelAction("wifi"))
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction("teleport"))
        assertEquals(Settings.ACTION_SETTINGS, settingsPanelAction(null))
    }

    // =====================================================================
    // Decision-9: broadened open_url (isSafeViewUri) + navigation
    // =====================================================================

    @Test
    fun `isSafeViewUri accepts http https AND app deep links`() {
        assertTrue(isSafeViewUri("https://example.com/x?q=1"))
        assertTrue(isSafeViewUri("http://example.com"))
        assertTrue(isSafeViewUri("  HTTPS://Example.com  ")) // trimmed, case-insensitive scheme
        assertTrue(isSafeViewUri("tel:18005551234"))
        assertTrue(isSafeViewUri("geo:0,0?q=coffee"))
        assertTrue(isSafeViewUri("mailto:a@b.com"))
        assertTrue(isSafeViewUri("sms:5551234"))
        assertTrue(isSafeViewUri("spotify:track:abc"))
        assertTrue(isSafeViewUri("myapp://open/thing"))
    }

    @Test
    fun `isSafeViewUri rejects file content intent javascript data and scheme-less`() {
        assertFalse(isSafeViewUri("file:///sdcard/secret.txt"))
        assertFalse(isSafeViewUri("content://com.other/private/1"))
        assertFalse(isSafeViewUri("intent://x#Intent;action=a;end")) // intent smuggling
        assertFalse(isSafeViewUri("javascript:alert(1)"))
        assertFalse(isSafeViewUri("data:text/html,hi"))
        assertFalse(isSafeViewUri("android_resource://com.other/1"))
        assertFalse(isSafeViewUri("example.com")) // no scheme
        assertFalse(isSafeViewUri(""))
        assertFalse(isSafeViewUri("   "))
        assertFalse(isSafeViewUri("https:")) // scheme but no scheme-specific part
    }

    @Test
    fun `navigationUri form-encodes the destination into a google navigation deep link`() {
        assertEquals("google.navigation:q=coffee+shop", navigationUri("coffee shop"))
        assertEquals("google.navigation:q=1600+Amphitheatre+Pkwy", navigationUri("1600 Amphitheatre Pkwy"))
        assertEquals("google.navigation:q=tea+%26+coffee", navigationUri("tea & coffee"))
    }

    // =====================================================================
    // Decision-9: guarded generic send_intent safety envelope
    // =====================================================================

    @Test
    fun `sendIntentRejectionReason allows a benign action with a safe or absent uri`() {
        assertNull(sendIntentRejectionReason("android.intent.action.VIEW", "https://example.com", null, null))
        assertNull(sendIntentRejectionReason("android.settings.SETTINGS", null, null, null))
        assertNull(sendIntentRejectionReason("com.example.CUSTOM", "myapp://x", "text/plain", "com.example"))
    }

    @Test
    fun `sendIntentRejectionReason requires a non-blank action`() {
        assertEquals("action required", sendIntentRejectionReason(null, null, null, null))
        assertEquals("action required", sendIntentRejectionReason("   ", null, null, null))
    }

    @Test
    fun `sendIntentRejectionReason rejects dangerous actions (case-insensitive)`() {
        assertEquals(
            "action not permitted via send_intent",
            sendIntentRejectionReason("android.intent.action.CALL", "tel:911", null, null),
        )
        assertEquals(
            "action not permitted via send_intent",
            sendIntentRejectionReason("ANDROID.INTENT.ACTION.CALL", "tel:911", null, null),
        )
        for (a in DANGEROUS_SEND_INTENT_ACTIONS) {
            assertEquals(
                "$a must be rejected",
                "action not permitted via send_intent",
                sendIntentRejectionReason(a, null, null, null),
            )
        }
    }

    @Test
    fun `sendIntentRejectionReason rejects unsafe uri schemes and scheme-less uris`() {
        assertEquals(
            "unsafe uri scheme",
            sendIntentRejectionReason("android.intent.action.VIEW", "file:///x", null, null),
        )
        assertEquals(
            "unsafe uri scheme",
            sendIntentRejectionReason("android.intent.action.VIEW", "content://x/1", null, null),
        )
        assertEquals(
            "unsafe uri scheme",
            sendIntentRejectionReason("android.intent.action.VIEW", "intent://x#Intent;end", null, null),
        )
        assertEquals(
            "uri scheme required",
            sendIntentRejectionReason("android.intent.action.VIEW", "example.com", null, null),
        )
    }
}
