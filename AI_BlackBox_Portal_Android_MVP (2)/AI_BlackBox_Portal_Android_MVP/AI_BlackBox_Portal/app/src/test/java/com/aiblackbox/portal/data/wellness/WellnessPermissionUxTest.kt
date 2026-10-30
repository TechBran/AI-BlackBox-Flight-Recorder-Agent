package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The Health Connect dialog asks for exactly what the user chose (spec §6.1 step 5): the read
 * permissions of the categories turned on, nothing else; past data and background only when
 * the user ticked them AND Health Connect says the feature is available. Never pre-selected.
 */
class WellnessPermissionUxTest {

    private val on = WellnessConsent.ASK
    private val off = WellnessConsent.OFF
    private val avail = HcFeatureStatus.AVAILABLE
    private val unavail = HcFeatureStatus.UNAVAILABLE

    private fun consent(sleep: WellnessConsent = off, heart: WellnessConsent = off, activity: WellnessConsent = off,
                        bp: WellnessConsent = off) = mapOf(
        WellnessCategory.SLEEP to sleep, WellnessCategory.HEART to heart,
        WellnessCategory.ACTIVITY to activity, WellnessCategory.BLOOD_PRESSURE to bp,
    )

    @Test
    fun `the requested set equals the chosen categories' read permissions`() {
        val requested = WellnessRules.permissionsToRequest(consent(sleep = on, activity = WellnessConsent.PROACTIVE),
            wantHistory = false, wantBackground = false, history = avail, background = avail)
        assertEquals(
            setOf(
                "android.permission.health.READ_SLEEP",
                "android.permission.health.READ_STEPS",
                "android.permission.health.READ_DISTANCE",
                "android.permission.health.READ_ACTIVE_CALORIES_BURNED",
                "android.permission.health.READ_TOTAL_CALORIES_BURNED",
                "android.permission.health.READ_EXERCISE",
            ),
            requested,
        )
    }

    @Test
    fun `defaults request sleep heart and activity but not blood pressure`() {
        val requested = WellnessRules.permissionsToRequest(WellnessCatalog.DEFAULT_CONSENT, false, false, avail, avail)
        assertFalse(HcType.BLOOD_PRESSURE.readPermission in requested)
        assertTrue(HcType.SLEEP.readPermission in requested)
        assertTrue(HcType.HEART_RATE.readPermission in requested)
        assertTrue(HcType.STEPS.readPermission in requested)
        assertEquals(11, requested.size)
    }

    @Test
    fun `blood pressure is requested only when turned on`() {
        val requested = WellnessRules.permissionsToRequest(consent(bp = on), false, false, avail, avail)
        assertEquals(setOf(HcType.BLOOD_PRESSURE.readPermission), requested)
    }

    @Test
    fun `history and background only when ticked and available`() {
        val c = consent(sleep = on)
        assertEquals(
            setOf(HcType.SLEEP.readPermission, WellnessCatalog.PERMISSION_HISTORY, WellnessCatalog.PERMISSION_BACKGROUND),
            WellnessRules.permissionsToRequest(c, wantHistory = true, wantBackground = true, history = avail, background = avail),
        )
        assertEquals(
            setOf(HcType.SLEEP.readPermission),
            WellnessRules.permissionsToRequest(c, wantHistory = true, wantBackground = true, history = unavail, background = unavail),
        )
        assertEquals(
            setOf(HcType.SLEEP.readPermission, WellnessCatalog.PERMISSION_HISTORY),
            WellnessRules.permissionsToRequest(c, wantHistory = true, wantBackground = false, history = avail, background = avail),
        )
    }

    @Test
    fun `nothing chosen asks for nothing, not even the specials`() {
        assertTrue(WellnessRules.permissionsToRequest(consent(), true, true, avail, avail).isEmpty())
    }

    @Test
    fun `the past-data and background cards show only when available`() {
        assertTrue(WellnessRules.showPastDataCard(avail))
        assertFalse(WellnessRules.showPastDataCard(unavail))
        assertTrue(WellnessRules.showBackgroundCard(avail))
        assertFalse(WellnessRules.showBackgroundCard(unavail))
    }

    @Test
    fun `no request ever names a write or the exercise route`() {
        val all = WellnessRules.permissionsToRequest(WellnessCategory.entries.associateWith { on }, true, true, avail, avail)
        assertEquals(14, all.size)
        assertTrue(all.none { "WRITE" in it || "ROUTE" in it })
    }

    @Test
    fun `readable types are consented AND granted`() {
        val granted = setOf(HcType.SLEEP.readPermission, HcType.HEART_RATE.readPermission, HcType.BLOOD_PRESSURE.readPermission)
        assertEquals(listOf(HcType.SLEEP), WellnessRules.readableTypes(consent(sleep = on), granted))
        assertEquals(listOf(HcType.SLEEP, HcType.HEART_RATE),
            WellnessRules.readableTypes(consent(sleep = on, heart = on), granted))
        assertTrue(WellnessRules.readableTypes(consent(activity = on), granted).isEmpty())
    }

    @Test
    fun `the probe asks for every category, past data and background only where available`() {
        val reads = WellnessCatalog.ALL_READ_PERMISSIONS
        assertEquals(reads + WellnessCatalog.PERMISSION_HISTORY + WellnessCatalog.PERMISSION_BACKGROUND,
            WellnessRules.probePermissions(avail, avail))
        assertEquals(reads, WellnessRules.probePermissions(unavail, unavail))
        assertEquals(reads + WellnessCatalog.PERMISSION_BACKGROUND, WellnessRules.probePermissions(unavail, avail))
    }
}
