package com.aiblackbox.portal.ui.settings

import com.aiblackbox.portal.data.local.InstalledModel
import com.aiblackbox.portal.data.model.LocalBundle
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Unit tests for the PURE picker reducer (Task W5.1): [modelRowsFrom] plus the
 * progress->percent helpers. Fully hermetic plain JUnit -- no Android, no IO, no
 * coroutines. Locks the merge precedence, the recommended-first sort/badge, and
 * the determinate/indeterminate progress mapping.
 */
class LocalModelRowTest {

    private val e2b = LocalBundle(
        slug = "gemma-4-e2b",
        displayName = "Gemma 4 E2B (on-device)",
        filename = "gemma-4-e2b-it.litertlm",
        sizeBytes = 3_000_000_000L,
        minRamGb = 3.0,
        recommended = false,
        contextNote = "Experimental -- weaker at multi-step agent loops",
    )
    private val e4b = LocalBundle(
        slug = "gemma-4-e4b",
        displayName = "Gemma 4 E4B (on-device)",
        filename = "gemma-4-e4b-it.litertlm",
        sizeBytes = 4_294_967_296L,
        minRamGb = 6.0,
        recommended = true,
        contextNote = "Recommended -- best on-device agent reliability",
    )

    private fun installed(slug: String) = InstalledModel(slug, File("/tmp/$slug"), 1L)

    // -- state precedence ---------------------------------------------------

    @Test
    fun `downloadable when in catalog and nothing else`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        assertEquals(1, rows.size)
        assertEquals(ModelRowState.Downloadable, rows.single().state)
    }

    @Test
    fun `installed beats downloadable when present on disk`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = listOf(installed("gemma-4-e2b")),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        val e2bRow = rows.first { it.slug == "gemma-4-e2b" }
        assertTrue("installed state", e2bRow.state is ModelRowState.Installed)
        assertFalse("not active (no activeSlug)", (e2bRow.state as ModelRowState.Installed).active)
    }

    @Test
    fun `installed model marked active when it is the active slug`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = listOf(installed("gemma-4-e4b")),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = "gemma-4-e4b",
        )
        val e4bRow = rows.first { it.slug == "gemma-4-e4b" }
        assertTrue((e4bRow.state as ModelRowState.Installed).active)
    }

    @Test
    fun `downloading shows the in-flight fraction and beats installed`() {
        // A model both installed AND mid-download (e.g. re-downloading) shows
        // DOWNLOADING -- the in-flight action wins.
        val rows = modelRowsFrom(
            catalog = listOf(e4b),
            installed = listOf(installed("gemma-4-e4b")),
            downloading = mapOf("gemma-4-e4b" to 0.42f),
            failed = emptySet(),
            activeSlug = null,
        )
        val state = rows.single().state
        assertTrue(state is ModelRowState.Downloading)
        assertEquals(0.42f, (state as ModelRowState.Downloading).progress, 0.0001f)
    }

    @Test
    fun `failed when in failed set and not installed or downloading`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = setOf("gemma-4-e2b"),
            activeSlug = null,
        )
        assertEquals(ModelRowState.Failed, rows.single().state)
    }

    @Test
    fun `installed clears a stale failed entry`() {
        // If a later install succeeded, INSTALLED must win over a leftover failed
        // flag for the same slug.
        val rows = modelRowsFrom(
            catalog = listOf(e2b),
            installed = listOf(installed("gemma-4-e2b")),
            downloading = emptyMap(),
            failed = setOf("gemma-4-e2b"),
            activeSlug = null,
        )
        assertTrue(rows.single().state is ModelRowState.Installed)
    }

    // -- recommended sort + badge -------------------------------------------

    @Test
    fun `recommended model sorts first regardless of catalog order`() {
        // Catalog order: E2B then E4B. E4B is recommended -> it must lead.
        val rows = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        assertEquals("gemma-4-e4b", rows.first().slug)
        assertTrue("recommended flag carried", rows.first().recommended)
        assertFalse("non-recommended not badged", rows.last().recommended)
    }

    @Test
    fun `non-recommended keep stable catalog order among themselves`() {
        val third = e2b.copy(slug = "gemma-x", displayName = "X", recommended = false)
        val rows = modelRowsFrom(
            catalog = listOf(e2b, third, e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        // E4B (recommended) first, then the two non-recommended in catalog order.
        assertEquals(listOf("gemma-4-e4b", "gemma-4-e2b", "gemma-x"), rows.map { it.slug })
    }

    @Test
    fun `context note is carried from the catalog bundle`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        assertEquals(
            "Recommended -- best on-device agent reliability",
            rows.first { it.slug == "gemma-4-e4b" }.contextNote,
        )
        assertEquals(
            "Experimental -- weaker at multi-step agent loops",
            rows.first { it.slug == "gemma-4-e2b" }.contextNote,
        )
    }

    @Test
    fun `blank context note becomes null`() {
        val rows = modelRowsFrom(
            catalog = listOf(e2b.copy(contextNote = "   ")),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        assertEquals(null, rows.single().contextNote)
    }

    // -- fitsRam display hint ------------------------------------------------

    @Test
    fun `fitsRam is null when no ram supplied`() {
        val rows = modelRowsFrom(
            catalog = listOf(e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
        )
        assertEquals(null, rows.single().fitsRam)
    }

    @Test
    fun `fitsRam true when device ram exceeds min and false otherwise`() {
        val gib = 1_073_741_824L
        // 8 GiB phone: E2B (3 GB) fits, E4B (6 GB) fits.
        val big = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
            ramBytes = 8 * gib,
        )
        assertEquals(true, big.first { it.slug == "gemma-4-e2b" }.fitsRam)
        assertEquals(true, big.first { it.slug == "gemma-4-e4b" }.fitsRam)

        // 4 GiB phone: E2B fits, E4B does NOT.
        val small = modelRowsFrom(
            catalog = listOf(e2b, e4b),
            installed = emptyList(),
            downloading = emptyMap(),
            failed = emptySet(),
            activeSlug = null,
            ramBytes = 4 * gib,
        )
        assertEquals(true, small.first { it.slug == "gemma-4-e2b" }.fitsRam)
        assertEquals(false, small.first { it.slug == "gemma-4-e4b" }.fitsRam)
    }

    // -- progress -> percent -------------------------------------------------

    @Test
    fun `progressToPercent floors the fraction and clamps to 0_100`() {
        assertEquals(0, progressToPercent(0L, 1000L))
        assertEquals(50, progressToPercent(500L, 1000L))
        assertEquals(99, progressToPercent(999L, 1000L))
        assertEquals(100, progressToPercent(1000L, 1000L))
        // Over-shoot clamps rather than exceeding 100.
        assertEquals(100, progressToPercent(2000L, 1000L))
    }

    @Test
    fun `progressToPercent is indeterminate when total unknown`() {
        assertEquals(-1, progressToPercent(0L, 0L))
        assertEquals(-1, progressToPercent(123L, -1L))
    }

    @Test
    fun `fractionToPercent maps 0_1 to 0_100 and negatives to indeterminate`() {
        assertEquals(0, fractionToPercent(0f))
        assertEquals(42, fractionToPercent(0.42f))
        assertEquals(100, fractionToPercent(1f))
        assertEquals(100, fractionToPercent(1.5f))
        assertEquals(-1, fractionToPercent(PROGRESS_INDETERMINATE))
        assertEquals(-1, fractionToPercent(-0.3f))
    }
}
