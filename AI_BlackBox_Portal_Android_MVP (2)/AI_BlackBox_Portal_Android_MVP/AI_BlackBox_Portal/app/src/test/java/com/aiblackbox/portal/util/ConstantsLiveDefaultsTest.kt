package com.aiblackbox.portal.util

import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test

class ConstantsLiveDefaultsTest {
    @Test fun `gemini live default is 3_1 preview`() {
        assertEquals(
            "gemini-3.1-flash-live-preview",
            Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        )
    }

    @Test fun `gemini live default is thinking capable`() {
        val default = Constants.LIVE_MODEL_DEFAULTS["gemini-live"]
        assertTrue(default in Constants.GEMINI_LIVE_THINKING_CAPABLE_MODELS)
    }

    @Test fun `gemini live default is present in model config list`() {
        val ids = Constants.MODEL_CONFIG["gemini-live"].orEmpty().map { it.first }
        assertTrue("gemini-3.1-flash-live-preview" in ids)
    }
}
