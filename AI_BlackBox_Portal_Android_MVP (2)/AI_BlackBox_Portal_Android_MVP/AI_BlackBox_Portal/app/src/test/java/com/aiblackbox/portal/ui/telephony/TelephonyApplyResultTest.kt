package com.aiblackbox.portal.ui.telephony

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The telephony wizard's Apply step returns the inbound-call PIN gate
 * (`phone_pin`). The PIN is in that one response only, so the wizard must show
 * it then; with no PIN set, inbound calls are refused and the wizard warns.
 * Mirrors Portal/modules/telephony-wizard.pin.test.mjs.
 */
class TelephonyApplyResultTest {

    private fun parse(body: String) = parseApplyResult(Json.parseToJsonElement(body).jsonObject)

    @Test
    fun generatedPinIsSurfacedOnce() {
        val r = parse(
            """{"applied":true,"reload":{"ok":true},"restart_recommended":false,
               "phone_pin":{"enabled":true,"configured":true,"generated":true,"length":6,"pin":"739104"}}"""
        )
        assertTrue(r.applied)
        assertTrue(r.reloadOk)
        assertEquals("739104", r.generatedPin)
        assertFalse(r.pinMissing)
        val (text, warn) = phonePinNotice(r)!!
        assertTrue(text.contains("739104"))
        assertFalse(warn)
    }

    @Test
    fun existingPinIsNeverShown() {
        val r = parse(
            """{"applied":true,"reload":{"ok":true},"restart_recommended":false,
               "phone_pin":{"enabled":true,"configured":true,"generated":false,"length":6}}"""
        )
        assertNull(r.generatedPin)
        assertNull(phonePinNotice(r))
    }

    @Test
    fun missingPinWarnsCallsAreRefused() {
        val r = parse(
            """{"applied":true,"reload":{"ok":false},"restart_recommended":true,
               "phone_pin":{"enabled":true,"configured":false,"generated":false,"length":0,"error":"x"}}"""
        )
        assertTrue(r.restartRecommended)
        assertTrue(r.pinMissing)
        val (text, warn) = phonePinNotice(r)!!
        assertTrue(text.contains("refused"))
        assertTrue(warn)
    }

    @Test
    fun disabledGateAndOlderBackendsStayQuiet() {
        val disabled = parse(
            """{"applied":true,"reload":{"ok":true},"restart_recommended":false,
               "phone_pin":{"enabled":false,"configured":false,"generated":false,"length":0}}"""
        )
        assertFalse(disabled.pinMissing)
        assertNull(phonePinNotice(disabled))
        val legacy = parse("""{"applied":true,"reload":{"ok":true},"restart_recommended":false}""")
        assertNull(legacy.generatedPin)
        assertFalse(legacy.pinMissing)
        assertNull(phonePinNotice(legacy))
    }
}
