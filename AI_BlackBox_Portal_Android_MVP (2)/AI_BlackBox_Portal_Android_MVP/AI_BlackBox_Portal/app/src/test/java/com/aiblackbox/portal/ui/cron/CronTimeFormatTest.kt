package com.aiblackbox.portal.ui.cron

import com.aiblackbox.portal.data.model.CronJobsResponse
import com.aiblackbox.portal.data.model.CronPreviewResponse
import kotlinx.serialization.json.Json
import org.junit.Assert.assertEquals
import org.junit.Test
import java.time.Instant
import java.time.ZoneId
import java.util.Locale

/**
 * Audit A12-voice-cron-14 (rank 53): cron times on the phone carry their zone. The box
 * stamps last_run_at in UTC and next_run_at in box-local time; the cards showed raw ISO
 * substrings of each, so "Last" and "Next" came from different zones with no label.
 */
class CronTimeFormatTest {

    private val ny = ZoneId.of("America/New_York")
    private val kolkata = ZoneId.of("Asia/Kolkata")

    @Test fun `UTC and box-local stamps land in the phone's zone, labelled`() {
        // The same instant written both ways by the box.
        val utc = "2026-10-23T11:00:00.123456+00:00"
        val boxLocal = "2026-10-23T07:00:00-04:00"
        assertEquals("Oct 23, 7:00 AM EDT", CronTimeFormat.absolute(utc, ny, Locale.US))
        assertEquals("Oct 23, 7:00 AM EDT", CronTimeFormat.absolute(boxLocal, ny, Locale.US))
        assertEquals("Oct 23, 4:30 PM IST", CronTimeFormat.absolute(boxLocal, kolkata, Locale.US))
    }

    @Test fun `a recent run reads as relative, a future one as absolute`() {
        val now = Instant.parse("2026-10-23T12:00:00Z")
        assertEquals("Just now", CronTimeFormat.card("2026-10-23T11:59:30+00:00", now, ny, Locale.US))
        assertEquals("12m ago", CronTimeFormat.card("2026-10-23T11:48:00+00:00", now, ny, Locale.US))
        assertEquals("3h ago", CronTimeFormat.card("2026-10-23T05:00:00-04:00", now, ny, Locale.US))
        assertEquals("Oct 24, 7:00 AM EDT", CronTimeFormat.card("2026-10-24T07:00:00-04:00", now, ny, Locale.US))
        assertEquals("Oct 21, 7:00 AM EDT", CronTimeFormat.card("2026-10-21T11:00:00+00:00", now, ny, Locale.US))
    }

    @Test fun `a string with no offset is shown as it is`() {
        assertEquals("2026-10-23T07:00:00", CronTimeFormat.absolute("2026-10-23T07:00:00", ny, Locale.US))
        assertEquals("", CronTimeFormat.card("", Instant.now(), ny, Locale.US))
    }

    @Test fun `the box zone rides along with the job list and the preview`() {
        val json = Json { ignoreUnknownKeys = true }
        val list = json.decodeFromString(CronJobsResponse.serializer(),
            """{"jobs":[],"count":0,"timezone":"America/New_York","tz_abbrev":"EDT"}""")
        assertEquals("EDT", list.tzAbbrev)
        assertEquals("America/New_York", list.timezone)
        val preview = json.decodeFromString(CronPreviewResponse.serializer(),
            """{"next_runs":["2026-10-24T07:00:00-04:00"],"timezone":"America/New_York","tz_abbrev":"EDT"}""")
        assertEquals("EDT", preview.tzAbbrev)
        // An older box sends neither field.
        assertEquals(null, json.decodeFromString(CronJobsResponse.serializer(), """{"jobs":[]}""").tzAbbrev)
    }
}
