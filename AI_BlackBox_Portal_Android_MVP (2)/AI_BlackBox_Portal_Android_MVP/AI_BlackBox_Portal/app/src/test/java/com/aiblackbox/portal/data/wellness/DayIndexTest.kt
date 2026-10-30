package com.aiblackbox.portal.data.wellness

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

class DayIndexTest {

    private val dir = WellnessFixtures.tempDir("dayindex")
    private val file = File(dir, "day_index.tsv")

    @Test
    fun `a deletion of a known id resolves to its type and day and is forgotten`() {
        val index = DayIndex(file)
        index.record("sleep-1", HcType.SLEEP, "2026-11-03")
        val res = index.resolveDeletion("sleep-1")
        assertEquals(DayIndex.Resolution.Known("sleep-1", DayIndex.Entry(HcType.SLEEP, "2026-11-03")), res)
        assertNull(index.lookup("sleep-1"))
    }

    @Test
    fun `an unknown id resolves to Unknown`() {
        assertEquals(DayIndex.Resolution.Unknown("never-seen"), DayIndex(file).resolveDeletion("never-seen"))
    }

    @Test
    fun `the index persists, holds no values and ignores malformed lines`() {
        val index = DayIndex(file)
        index.record("bp-1", HcType.BLOOD_PRESSURE, "2026-11-01")
        index.record("hr-1", HcType.HEART_RATE, "2026-11-02")
        index.save()
        val text = file.readText()
        assertEquals("bp-1\tblood_pressure\t2026-11-01\nhr-1\theart_rate\t2026-11-02\n", text)
        // Only ids, type names and dates: no number that could be a reading.
        assertFalse(text.contains("118") || text.contains("79"))
        file.appendText("junk line\nx\tnot_a_type\t2026-11-01\ny\tsleep\tbad-day\n")
        val reopened = DayIndex(file)
        assertEquals(2, reopened.size)
        assertEquals(DayIndex.Entry(HcType.BLOOD_PRESSURE, "2026-11-01"), reopened.lookup("bp-1"))
    }

    @Test
    fun `re-recording an id moves it to the new day`() {
        val index = DayIndex(file)
        index.record("ex-1", HcType.EXERCISE, "2026-11-01")
        index.record("ex-1", HcType.EXERCISE, "2026-11-02")
        assertEquals("2026-11-02", index.lookup("ex-1")!!.day)
    }

    @Test
    fun `pruning drops only old bucketed entries, sessions and readings are kept at any age`() {
        val index = DayIndex(file)
        index.record("hr-old", HcType.HEART_RATE, "2025-01-01")
        index.record("steps-new", HcType.STEPS, "2026-11-01")
        index.record("sleep-old", HcType.SLEEP, "2024-06-01")
        index.record("bp-old", HcType.BLOOD_PRESSURE, "2024-06-01")
        index.pruneBucketed("2026-07-01")
        assertNull(index.lookup("hr-old"))
        assertTrue(index.lookup("steps-new") != null)
        assertTrue(index.lookup("sleep-old") != null)
        assertTrue(index.lookup("bp-old") != null)
    }

    @Test
    fun `ids with tabs or newlines are refused and clear empties the index`() {
        val index = DayIndex(file)
        index.record("bad\tid", HcType.SLEEP, "2026-11-01")
        index.record("bad\nid", HcType.SLEEP, "2026-11-01")
        assertEquals(0, index.size)
        index.record("ok", HcType.SLEEP, "2026-11-01")
        index.save()
        index.clear()
        assertEquals(0, index.size)
        assertFalse(file.exists())
    }
}
