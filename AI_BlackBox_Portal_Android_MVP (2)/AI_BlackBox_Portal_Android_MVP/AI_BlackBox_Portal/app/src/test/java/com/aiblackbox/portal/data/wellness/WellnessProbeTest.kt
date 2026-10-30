package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.int
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/** The M7.1 probe report: every T-test present, nothing sent, no health values. */
class WellnessProbeTest {

    private val packages = object : PackageInfoSource {
        override fun label(packageName: String) = if (packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE) "Samsung Health" else null
        override fun versionName(packageName: String) = "6.31.0.001".takeIf { packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE }
        override fun isInstalled(packageName: String) = packageName == WellnessCatalog.SAMSUNG_HEALTH_PACKAGE
    }

    private val env = object : WellnessProbe.Environment {
        override fun deviceNameReadable() = false
        override fun batteryExempt() = false
        override fun tailnetIp() = "100.64.0.42"
    }

    @Test
    fun `the report covers T-1 to T-11 with counts and dates only`() = runBlocking {
        val gw = FakeHealthConnectGateway()
        for (day in 2..4) {
            gw.addHeartRate(WellnessFixtures.meta("hr-$day"), WellnessFixtures.OFFSET_EST,
                WellnessFixtures.hrSeries(at(day - 1, 23, 30), at(day, 2), 60, 50.0))
            gw.upsert(WellnessFixtures.sleepNight("sleep-$day", day))
        }
        gw.upsert(WellnessFixtures.bp("bp-1", 4, 7))
        val state = WellnessSyncState(
            runLog = listOf(RunLogEntry(at(4, 1), false, "ok"), RunLogEntry(at(4, 3), false, "ok"), RunLogEntry(at(4, 4), true, "ok")),
            deletionsSeen = 2, sleepLatencyMin = listOf(35, 90),
        )
        val report = WellnessProbe(gw, packages, { PhoneInfo("samsung", "SM-TEST0", "x", 36, "t") }, { state }, env,
            clock = { at(5, 9) }, zone = { WellnessFixtures.ZONE }).run()
        val keys = report.keys.filter { it.startsWith("t") }
        assertEquals(listOf("t1_grants_and_background", "t2_backfill_depth", "t3_heart_rate_density", "t4_metadata",
            "t5_blood_pressure_chain", "t6_samsung_export", "t7_grant_labels", "t8_device_name_and_deletions", "t9_scheduling",
            "t10_this_phone", "t11_sleep_arrival"), keys)
        assertFalse(report.getValue("sent_to_box").jsonPrimitive.boolean)
        val t1 = report.getValue("t1_grants_and_background").jsonObject
        assertTrue(t1.getValue("background_granted").jsonPrimitive.boolean)
        assertEquals(2, t1.getValue("periodic_runs_screen_off").jsonPrimitive.int)
        assertEquals("2026-11-01", report.getValue("t2_backfill_depth").jsonObject.getValue("earliest_samsung_sleep").jsonPrimitive.content)
        val t3 = report.getValue("t3_heart_rate_density").jsonObject
        assertEquals(3, t3.getValue("sleep_sessions_with_hr").jsonPrimitive.int)
        assertEquals("continuous", t3.getValue("origins").jsonArray[0].jsonObject.getValue("hr_mode").jsonPrimitive.content)
        assertEquals("1/1", report.getValue("t4_metadata").jsonObject.getValue("bp_measurement_location_filled").jsonPrimitive.content)
        assertEquals(listOf(120L, 60L), report.getValue("t9_scheduling").jsonObject.getValue("periodic_gaps_min").jsonArray
            .map { it.jsonPrimitive.content.toLong() })
        assertEquals(2, report.getValue("t8_device_name_and_deletions").jsonObject.getValue("deletion_changes_seen").jsonPrimitive.int)
        assertFalse(report.getValue("t6_samsung_export").jsonObject.getValue("automatable").jsonPrimitive.boolean)
        // No value leaks: the sentinel blood pressure appears nowhere.
        val text = report.toString()
        assertFalse(text.contains("118") || text.contains("\"79"))
    }
}
