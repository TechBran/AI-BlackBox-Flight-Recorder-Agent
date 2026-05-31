package com.aiblackbox.portal.ui.voice

import org.junit.Assert.assertEquals
import org.junit.Test

class AudioAmplitudeTest {
    @Test fun `silence is zero`() {
        assertEquals(0f, rmsAmplitude(ShortArray(256), 256), 0.0001f)
    }

    @Test fun `full scale is approximately one`() {
        val buf = ShortArray(256) { Short.MAX_VALUE }
        assertEquals(1f, rmsAmplitude(buf, 256), 0.001f)
    }

    @Test fun `half scale is approximately one half`() {
        val buf = ShortArray(256) { (Short.MAX_VALUE / 2).toShort() }
        assertEquals(0.5f, rmsAmplitude(buf, 256), 0.01f)
    }

    @Test fun `zero count returns zero and does not divide by zero`() {
        val buf = ShortArray(256) { Short.MAX_VALUE }
        assertEquals(0f, rmsAmplitude(buf, 0), 0.0001f)
    }

    @Test fun `respects count smaller than buffer`() {
        val buf = ShortArray(256)
        for (i in 0 until 4) buf[i] = Short.MAX_VALUE
        assertEquals(1f, rmsAmplitude(buf, 4), 0.001f)
    }
}
