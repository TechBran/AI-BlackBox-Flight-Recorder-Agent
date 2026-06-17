package com.aiblackbox.portal.overlay

import android.provider.Settings
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
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
}
