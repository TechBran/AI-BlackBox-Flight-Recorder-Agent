package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.data.store.BlackBoxStore
import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * The persistence CONTRACT for the waveform style, at the JVM layer that does not
 * need an Android DataStore instance: the preference KEY name (byte-identical
 * forever, like the ids) and the DEFAULT the store falls back to.
 *
 * BlackBoxStore.waveformStyle maps `it[KEY_WAVEFORM_STYLE]` through
 * [WaveformStyles.parse], so an ABSENT key resolves to `parse(null)` = the default
 * (aurora). Pinning `parse(null)` here pins the store's effective default without
 * standing up DataStore; the key-name assertion catches an accidental rename that
 * would silently strand every user who saved a choice.
 */
class WaveformStyleStoreTest {

    @Test
    fun the_persisted_key_name_is_stable() {
        assertEquals("waveform_style", BlackBoxStore.KEY_WAVEFORM_STYLE.name)
    }

    @Test
    fun an_absent_or_garbage_stored_value_defaults_to_aurora() {
        // The exact transform BlackBoxStore.waveformStyle applies to the raw value.
        assertEquals("aurora", WaveformStyles.parse(null))
        assertEquals("aurora", WaveformStyles.parse(""))
        assertEquals("aurora", WaveformStyles.parse("no_such_style"))
    }

    @Test
    fun a_known_stored_value_round_trips_unchanged() {
        for (id in WaveformStyles.ids) {
            assertEquals(id, WaveformStyles.parse(id))
        }
    }
}
