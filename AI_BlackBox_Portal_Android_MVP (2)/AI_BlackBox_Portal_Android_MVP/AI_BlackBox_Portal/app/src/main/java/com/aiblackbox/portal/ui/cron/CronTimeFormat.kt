package com.aiblackbox.portal.ui.cron

import java.time.Duration
import java.time.Instant
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.util.Locale

/**
 * Cron times on the phone, always with their zone (A12-voice-cron-14).
 *
 * The box writes last_run_at in UTC and next_run_at in box-local time, both as ISO
 * strings with an offset. The cards used to show substring(5, 16) of the raw string,
 * so "Last" and "Next" were wall-clock values from different zones with no label, and
 * the Portal (which converts to the viewer's zone) showed other times. Every time here
 * is converted to the phone's zone and labelled with it; a time within the last day
 * reads as relative ("5m ago"), like the Portal.
 */
object CronTimeFormat {

    /** "Oct 24, 7:00 AM EDT" in [zone]; the raw string when it has no offset to place it. */
    fun absolute(iso: String, zone: ZoneId = ZoneId.systemDefault(), locale: Locale = Locale.getDefault()): String {
        val instant = instantOf(iso) ?: return iso
        return DateTimeFormatter.ofPattern("MMM d, h:mm a z", locale).format(instant.atZone(zone))
    }

    /** "Just now" / "12m ago" / "3h ago" within the last day, else [absolute]. */
    fun card(
        iso: String,
        now: Instant = Instant.now(),
        zone: ZoneId = ZoneId.systemDefault(),
        locale: Locale = Locale.getDefault(),
    ): String {
        val instant = instantOf(iso) ?: return iso
        val ago = Duration.between(instant, now)
        return when {
            ago.isNegative || ago >= Duration.ofDays(1) -> absolute(iso, zone, locale)
            ago < Duration.ofMinutes(1) -> "Just now"
            ago < Duration.ofHours(1) -> "${ago.toMinutes()}m ago"
            else -> "${ago.toHours()}h ago"
        }
    }

    private fun instantOf(iso: String): Instant? =
        try {
            OffsetDateTime.parse(iso.trim()).toInstant()
        } catch (_: Exception) {
            null
        }
}
