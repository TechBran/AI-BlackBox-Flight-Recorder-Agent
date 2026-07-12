package com.aiblackbox.portal.util

import com.aiblackbox.portal.data.voice.VoiceSessionConfig
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/** P6a — translation voice mode: language catalog + config-field invariants. */
class TranslateLanguagesTest {

    private val bcp47 = Regex("^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})*$")

    @Test
    fun catalogHasTwentyEntries() {
        assertEquals(20, Constants.TRANSLATE_LANGUAGES.size)
    }

    @Test
    fun allIdsAreWellFormedBcp47() {
        Constants.TRANSLATE_LANGUAGES.forEach { (id, label) ->
            assertTrue("bad tag: $id", bcp47.matches(id))
            assertTrue("empty label for $id", label.isNotBlank())
        }
    }

    @Test
    fun sessionConfigTranslateFieldsDefaultOff() {
        val cfg = VoiceSessionConfig()
        assertNull(cfg.mode)
        assertNull(cfg.targetLanguage)
    }
}
