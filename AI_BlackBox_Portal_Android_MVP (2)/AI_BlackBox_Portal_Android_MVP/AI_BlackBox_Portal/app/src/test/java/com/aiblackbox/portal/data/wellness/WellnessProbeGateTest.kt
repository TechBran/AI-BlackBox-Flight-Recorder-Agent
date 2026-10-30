package com.aiblackbox.portal.data.wellness

import com.aiblackbox.portal.BuildConfig
import org.junit.Assert.assertFalse
import org.junit.Test

/**
 * The M7.1 Health probe asks Health Connect for every read permission. A customer's release
 * build must never offer it: it exists in debug builds and in a build made with
 * `-PwellnessProbe` (the owner's device walk), never by default and never by a tap pattern.
 */
class WellnessProbeGateTest {

    @Test
    fun `the probe flag is off unless the build asked for it`() {
        assertFalse(BuildConfig.WELLNESS_PROBE)
        assertFalse(BuildConfig.VERSION_NAME.contains("health-probe"))
    }
}
