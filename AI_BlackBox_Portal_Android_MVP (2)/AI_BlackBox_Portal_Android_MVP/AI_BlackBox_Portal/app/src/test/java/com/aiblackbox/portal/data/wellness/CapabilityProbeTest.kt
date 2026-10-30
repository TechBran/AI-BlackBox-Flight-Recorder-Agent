package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.data.wellness.WellnessFixtures.at
import kotlinx.coroutines.runBlocking
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class CapabilityProbeTest {

    private class FakePackages(
        private val labels: Map<String, String>,
        private val versions: Map<String, String> = emptyMap(),
    ) : PackageInfoSource {
        override fun label(packageName: String) = labels[packageName]
        override fun versionName(packageName: String) = versions[packageName]
        override fun isInstalled(packageName: String) = packageName in labels
    }

    private val phone = PhoneInfo("samsung", "SM-TEST0", "Fixture phone", 36, "1.14.0-test")
    private val now = at(5, 9)

    private fun populated(): FakeHealthConnectGateway {
        val gw = FakeHealthConnectGateway()
        // Watch: continuous heart rate (every minute) for two hours a night, three nights.
        for (day in 2..4) {
            gw.addHeartRate(WellnessFixtures.meta("hr-$day"), WellnessFixtures.OFFSET_EST,
                WellnessFixtures.hrSeries(at(day, 1), at(day, 3), 60, 55.0))
            gw.upsert(WellnessFixtures.sleepNight("sleep-$day", day))
        }
        // Phone: steps, recorded by the phone itself.
        gw.upsert(WellnessFixtures.steps("steps-1", at(4, 9), at(4, 10)), 1000.0)
        gw.upsert(WellnessFixtures.steps("steps-2", at(4, 12), at(4, 13)), 500.0)
        // A cuff app writing manual blood pressure, with no device in the metadata.
        gw.upsert(WellnessFixtures.bp("bp-1", 4, 7))
        // Older than the 14-day window: not counted.
        gw.upsert(WellnessFixtures.sleepNight("sleep-old", 3).copy(startMs = at(15, 23, month = 10), endMs = at(16, 6, month = 10)))
        return gw
    }

    private val packages = FakePackages(
        mapOf(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE to "Samsung Health", WellnessCatalog.HEALTH_CONNECT_PACKAGE to "Health Connect"),
        mapOf(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE to "6.31.0.001"),
    )

    @Test
    fun `origins are grouped by app x device x recording method with counts only`() = runBlocking {
        val result = CapabilityProbe(populated(), packages, { phone }, { now }).run()
        val origins = result.report.origins
        assertEquals(3, origins.size)
        val cuff = origins.first { it.packageName == "com.example.cuffapp" }
        assertEquals("com.example.cuffapp", cuff.label) // no PackageManager label: the package
        assertNull(cuff.deviceType)
        assertEquals("manual", cuff.recordingMethod)
        assertEquals(listOf("bp_systolic", "bp_diastolic"), cuff.metrics.map { it.metric })
        assertEquals(1, cuff.metrics[0].count14d)

        val phoneOrigin = origins.first { it.deviceType == "PHONE" }
        assertEquals("Samsung Health", phoneOrigin.label)
        assertEquals(listOf("steps"), phoneOrigin.metrics.map { it.metric })
        assertEquals(2, phoneOrigin.metrics[0].count14d)
        assertEquals(3 * 3600L, phoneOrigin.metrics[0].medianGapS)

        val watch = origins.first { it.deviceType == "WATCH" }
        val metrics = watch.metrics.associateBy { it.metric }
        assertEquals(3, metrics.getValue("sleep_duration").count14d) // the October night is outside the window
        assertEquals(3, metrics.getValue("sleep_stages").count14d)
        assertEquals(3 * 120, metrics.getValue("heart_rate").count14d)
        assertEquals(60L, metrics.getValue("heart_rate").medianGapS)
        assertEquals(IngestPayloadBuilder.iso(at(4, 2, 59)), metrics.getValue("heart_rate").lastSample)
    }

    @Test
    fun `heart-rate mode, the auto origin and the samsung line`() = runBlocking {
        val result = CapabilityProbe(populated(), packages, { phone }, { now }).run()
        assertEquals("continuous", result.hrModeByPackage[WellnessCatalog.SAMSUNG_HEALTH_PACKAGE])
        assertEquals(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE, result.autoHeartRateOrigin)
        assertEquals("WATCH", result.autoHeartRateDevice?.deviceType)
        assertTrue(result.samsungWritesToHc)
        assertTrue(result.report.samsungHealth.installed)
        assertEquals("6.31.0.001", result.report.samsungHealth.version)
    }

    @Test
    fun `manual heart rate is detected from sparse samples`() = runBlocking {
        val gw = FakeHealthConnectGateway()
        for (day in 1..4) gw.addHeartRate(WellnessFixtures.meta("hr-$day"), WellnessFixtures.OFFSET_EST,
            WellnessFixtures.hrSeries(at(day, 8), at(day, 20), 3600, 70.0))
        val result = CapabilityProbe(gw, packages, { phone }, { now }).run()
        assertEquals("manual", result.hrModeByPackage[WellnessCatalog.SAMSUNG_HEALTH_PACKAGE])
    }

    @Test
    fun `only granted types are probed and an unavailable Health Connect reports nothing`() = runBlocking {
        val gw = populated().apply { granted = setOf(HcType.STEPS.readPermission) }
        val result = CapabilityProbe(gw, packages, { phone }, { now }).run()
        assertEquals(listOf("steps"), result.report.origins.flatMap { o -> o.metrics.map { it.metric } })
        assertEquals(listOf(HcType.STEPS.readPermission), result.report.healthConnect.granted)

        val down = populated().apply { sdk = HcSdkStatus.NEEDS_UPDATE }
        val none = CapabilityProbe(down, packages, { phone }, { now }).run()
        assertEquals("needs_update", none.report.healthConnect.sdkStatus)
        assertTrue(none.report.origins.isEmpty())
        assertEquals("unavailable", none.report.healthConnect.backgroundFeature)
        assertFalse(none.samsungWritesToHc)
    }

    @Test
    fun `the report has exactly the contract's fields and no values (golden)`() = runBlocking {
        val result = CapabilityProbe(populated(), packages, { phone }, { now }).run()
        val json = CapabilityProbe.toJson(result.report)
        val obj = Json.parseToJsonElement(json).jsonObject
        assertEquals(listOf("phone", "health_connect", "samsung_health", "origins", "reported_at"), obj.keys.toList())
        assertEquals(listOf("manufacturer", "model", "device_name", "sdk_int", "app_version"),
            obj.getValue("phone").jsonObject.keys.toList())
        assertEquals(listOf("sdk_status", "background_feature", "history_feature", "granted"),
            obj.getValue("health_connect").jsonObject.keys.toList())
        // The sentinel blood pressure (118/79) never appears: counts only.
        assertFalse(json.contains("118") || json.contains("79.0"))
        // The golden is the exact wire form plus a trailing newline.
        val golden = json + "\n"
        val expected = javaClass.classLoader!!.getResource("wellness/capability_report.json")?.readText()
        if (expected != golden) {
            val out = File("build/wellness-goldens").apply { mkdirs() }
            File(out, "capability_report.json").writeText(golden)
            throw AssertionError("golden capability_report.json differs; actual in build/wellness-goldens/")
        }
    }

    @Test
    fun `median gap`() {
        assertNull(CapabilityProbe.medianGapS(listOf(5L)))
        assertEquals(60L, CapabilityProbe.medianGapS(listOf(0L, 60_000L, 120_000L)))
        assertEquals(90L, CapabilityProbe.medianGapS(listOf(0L, 60_000L, 180_000L)))
        assertEquals(0L, CapabilityProbe.medianGapS(listOf(5L, 5L)))
    }
}
