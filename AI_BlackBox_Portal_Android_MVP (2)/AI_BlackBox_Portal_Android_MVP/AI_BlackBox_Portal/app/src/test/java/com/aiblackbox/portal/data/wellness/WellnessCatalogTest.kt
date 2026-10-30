package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.boolean
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * The Kotlin catalog and its `res/raw/wellness_catalog.json` mirror say the same thing, and
 * both match the wire contract: four categories, the sixteen v1 metrics in contract order,
 * read-only types, no exercise route.
 */
class WellnessCatalogTest {

    private fun rawCatalog(): File {
        for (rel in listOf("src/main/res/raw/wellness_catalog.json", "app/src/main/res/raw/wellness_catalog.json")) {
            val f = File(rel)
            if (f.exists()) return f
        }
        throw AssertionError("wellness_catalog.json not found from ${File("").absolutePath}")
    }

    private val json by lazy { Json.parseToJsonElement(rawCatalog().readText()).jsonObject }

    @Test
    fun `categories and metrics are the wire contract's, in order`() {
        assertEquals(listOf("sleep", "heart", "activity", "blood_pressure"), WellnessCategory.entries.map { it.wire })
        assertEquals(
            listOf(
                "sleep_duration", "sleep_stages", "sleeping_hr_median", "sleeping_hr_p10", "resting_hr", "heart_rate",
                "hrv", "spo2", "respiratory_rate", "steps", "distance", "active_calories", "total_calories", "exercise",
                "bp_systolic", "bp_diastolic",
            ),
            WellnessCatalog.METRICS.map { it.id },
        )
    }

    @Test
    fun `the raw JSON mirrors the Kotlin catalog`() {
        assertEquals(WellnessCatalog.SCHEMA, json.getValue("schema").jsonPrimitive.content)
        val cats = json.getValue("categories").jsonArray.map { it.jsonObject }
        assertEquals(WellnessCategory.entries.map { it.wire }, cats.map { it.getValue("id").jsonPrimitive.content })
        for (c in cats) {
            val cat = WellnessCategory.fromWire(c.getValue("id").jsonPrimitive.content)!!
            assertEquals(cat.label, c.getValue("label").jsonPrimitive.content)
            assertEquals(WellnessCatalog.DEFAULT_CONSENT.getValue(cat).wire, c.getValue("default_consent").jsonPrimitive.content)
            assertEquals(WellnessCatalog.DEFAULT_LOCAL_ONLY.getValue(cat), c.getValue("default_local_only").jsonPrimitive.boolean)
            assertEquals(
                WellnessCatalog.TYPES_BY_CATEGORY.getValue(cat).map { it.wire },
                c.getValue("hc_types").jsonArray.map { it.jsonPrimitive.content },
            )
        }
        val types = json.getValue("hc_types").jsonArray.map { it.jsonObject }
        assertEquals(HcType.entries.map { it.wire }, types.map { it.getValue("id").jsonPrimitive.content })
        for (t in types) {
            val type = HcType.fromWire(t.getValue("id").jsonPrimitive.content)!!
            assertEquals(type.recordName, t.getValue("record").jsonPrimitive.content)
            assertEquals(type.readPermission, t.getValue("permission").jsonPrimitive.content)
            assertEquals(type.shape.name.lowercase(), t.getValue("shape").jsonPrimitive.content)
            assertEquals(WellnessCatalog.categoryOf(type).wire, t.getValue("category").jsonPrimitive.content)
        }
        val metrics = json.getValue("metrics").jsonArray.map { it.jsonObject }
        assertEquals(WellnessCatalog.METRICS.size, metrics.size)
        for ((m, j) in WellnessCatalog.METRICS.zip(metrics)) {
            assertEquals(m.id, j.getValue("id").jsonPrimitive.content)
            assertEquals(m.category.wire, j.getValue("category").jsonPrimitive.content)
            assertEquals(m.kind.wire, j.getValue("kind").jsonPrimitive.content)
            assertEquals(m.sources.map { it.wire }, j.getValue("sources").jsonArray.map { it.jsonPrimitive.content })
        }
        val special = json.getValue("special_permissions").jsonObject
        assertEquals(WellnessCatalog.PERMISSION_HISTORY, special.getValue("history").jsonPrimitive.content)
        assertEquals(WellnessCatalog.PERMISSION_BACKGROUND, special.getValue("background").jsonPrimitive.content)
    }

    @Test
    fun `every type belongs to exactly one category and permissions are read-only`() {
        for (type in HcType.entries) {
            assertEquals(type.wire, 1, WellnessCatalog.TYPES_BY_CATEGORY.values.count { type in it })
            assertTrue(type.readPermission.startsWith("android.permission.health.READ_"))
        }
        assertFalse(WellnessCatalog.ALL_READ_PERMISSIONS.any { "ROUTE" in it || "WRITE" in it })
    }

    @Test
    fun `defaults are sleep heart activity on and blood pressure off and local-only`() {
        assertEquals(WellnessConsent.OFF, WellnessCatalog.DEFAULT_CONSENT[WellnessCategory.BLOOD_PRESSURE])
        assertTrue(WellnessCatalog.DEFAULT_LOCAL_ONLY.getValue(WellnessCategory.BLOOD_PRESSURE))
        for (c in listOf(WellnessCategory.SLEEP, WellnessCategory.HEART, WellnessCategory.ACTIVITY)) {
            assertEquals(WellnessConsent.ASK, WellnessCatalog.DEFAULT_CONSENT[c])
        }
    }
}
