package com.aiblackbox.portal.ui.components.wave

import com.aiblackbox.portal.ui.components.aurora.AuroraSpeaker
import com.aiblackbox.portal.ui.components.aurora.AuroraVoice
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The waveform REGISTRY's invariants, mirroring FieldRegistry's discipline — the
 * catalogue is the single source of truth for the style list, so these pin the
 * things a silent break would corrupt: the persisted ids (forever), the display
 * order, the fallback-to-first, and that every entry actually resolves and
 * dispatches. All JVM-pure; the renderers are constructed but not drawn (no
 * DrawScope on the JVM), which is enough to prove the dispatch wiring.
 */
class WaveformRegistryTest {

    /**
     * THE persisted-id contract, in display order. These strings are written to
     * DataStore and read back by every future build; changing one silently
     * strands every user who saved it. Pinned here so a rename is a failed test,
     * not a field bug. Aurora is FIRST (default + fallback).
     */
    private val expectedIdsInOrder = listOf(
        "aurora",
        "classic",
        "ribbon",
        "mirrored_pulse",
        "segmented_eq",
        "soft_blob",
        "sharp_line",
        "orb",
        "particle_wave",
        "cinematic",
    )

    private val expectedLabels = mapOf(
        "aurora" to "Aurora Bands",
        "classic" to "Classic Lines",
        "ribbon" to "Glowing Ribbon",
        "mirrored_pulse" to "Mirrored Pulse",
        "segmented_eq" to "Segmented Equalizer",
        "soft_blob" to "Soft Blob Wave",
        "sharp_line" to "Sharp Reactive Line",
        "orb" to "Circular Orb",
        "particle_wave" to "Particle Wave",
        "cinematic" to "Cinematic Glow",
    )

    @Test
    fun catalogue_has_exactly_ten_entries() {
        assertEquals(10, WaveformStyles.CATALOGUE.size)
        assertEquals(10, WaveformStyles.ids.size)
    }

    @Test
    fun ids_are_unique() {
        assertEquals(
            "duplicate id in the catalogue",
            WaveformStyles.ids.size,
            WaveformStyles.ids.toSet().size,
        )
    }

    @Test
    fun ids_match_the_pinned_strings_in_display_order() {
        assertEquals(expectedIdsInOrder, WaveformStyles.ids)
    }

    @Test
    fun aurora_is_first_and_is_the_default_and_fallback() {
        assertEquals("aurora", WaveformStyles.CATALOGUE[0].id)
        assertEquals("aurora", WaveformStyles.default.id)
        // Unknown / absent / garbage → the default, never a crash.
        assertEquals("aurora", WaveformStyles.resolve(null).id)
        assertEquals("aurora", WaveformStyles.resolve("").id)
        assertEquals("aurora", WaveformStyles.resolve("   ").id)
        assertEquals("aurora", WaveformStyles.resolve("no_such_style").id)
        assertEquals("aurora", WaveformStyles.parse("garbage"))
    }

    @Test
    fun resolve_is_case_and_whitespace_insensitive() {
        assertEquals("aurora", WaveformStyles.resolve("AURORA").id)
        assertEquals("classic", WaveformStyles.resolve("  Classic  ").id)
        assertEquals("mirrored_pulse", WaveformStyles.resolve("MIRRORED_PULSE").id)
    }

    @Test
    fun every_id_resolves_back_to_its_own_entry() {
        for (entry in WaveformStyles.CATALOGUE) {
            val resolved = WaveformStyles.resolve(entry.id)
            assertSame("resolve(${entry.id}) returned a different entry", entry, resolved)
            assertEquals(entry.id, WaveformStyles.parse(entry.id))
        }
    }

    @Test
    fun labels_match_the_pinned_strings() {
        for (entry in WaveformStyles.CATALOGUE) {
            assertEquals(expectedLabels[entry.id], entry.label)
            assertEquals(expectedLabels[entry.id], WaveformStyles.label(entry.id))
        }
    }

    /**
     * The DISPATCH invariant: every entry is one of the two known content kinds,
     * every Draw entry's factory produces a usable renderer, and the one
     * SelfContained entry (aurora) carries a non-null composable. This is the
     * JVM-testable core of "VoiceVisualizer can dispatch every catalogue entry".
     */
    @Test
    fun every_entry_dispatches() {
        var selfContained = 0
        var draw = 0
        for (entry in WaveformStyles.CATALOGUE) {
            when (val c = entry.content) {
                is WaveformContent.SelfContained -> {
                    assertNotNull("null SelfContained content for ${entry.id}", c.content)
                    selfContained++
                }
                is WaveformContent.Draw -> {
                    val renderer = c.create()
                    assertNotNull("Draw.create() returned null for ${entry.id}", renderer)
                    draw++
                }
            }
        }
        // Aurora is the only self-contained style; the other nine are draw styles.
        assertEquals(1, selfContained)
        assertEquals(9, draw)
    }

    @Test
    fun draw_renderers_default_needsFrames_tracks_audibility() {
        val feed = WaveformFeed()
        val silent = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(0f, 0f, 0f, 0f))
        val loud = AuroraVoice(AuroraSpeaker.HUMAN, floatArrayOf(1f, 1f, 1f, 1f))
        for (entry in WaveformStyles.CATALOGUE) {
            val c = entry.content
            if (c !is WaveformContent.Draw) continue
            val r = c.create()
            assertFalse("${entry.id} should not need frames with no voices", r.needsFrames(emptyList(), feed))
            assertFalse("${entry.id} should not need frames while silent", r.needsFrames(listOf(silent), feed))
            assertTrue("${entry.id} should need frames while audible", r.needsFrames(listOf(loud), feed))
        }
    }
}
