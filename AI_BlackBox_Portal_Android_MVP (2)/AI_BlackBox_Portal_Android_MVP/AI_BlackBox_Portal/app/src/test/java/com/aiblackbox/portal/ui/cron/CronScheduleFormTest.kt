package com.aiblackbox.portal.ui.cron

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Assert.fail
import org.junit.Test
import java.io.File

/**
 * Audit A12-voice-cron-01 / -15 (rank 53): opening a cron job in the editor and saving
 * it must never change when it runs. The Simple form takes a schedule only when it
 * rebuilds exactly the same expression; everything else stays in Advanced, verbatim.
 * Weekly has a real weekday (standard cron, 0 = Sunday), and an hour or minute out of
 * range builds nothing instead of "0 99 * * *".
 */
class CronScheduleFormTest {

    private val form = CronScheduleForm

    @Test fun `every Simple shape round-trips exactly`() {
        val shapes = listOf(
            CronScheduleForm.Simple(CronScheduleForm.HOURLY),
            CronScheduleForm.Simple(CronScheduleForm.DAILY, 7, 0),
            CronScheduleForm.Simple(CronScheduleForm.DAILY, 0, 0),
            CronScheduleForm.Simple(CronScheduleForm.DAILY, 23, 59),
        ) + (0..6).map { CronScheduleForm.Simple(CronScheduleForm.WEEKLY, 8, 30, it) }
        for (s in shapes) {
            val expr = form.build(s)!!
            val back = form.parse(expr)!!
            assertEquals(expr, form.build(back))
            assertEquals(s.frequency, back.frequency)
            if (s.frequency != CronScheduleForm.HOURLY) {
                assertEquals(s.hour, back.hour)
                assertEquals(s.minute, back.minute)
            }
            if (s.frequency == CronScheduleForm.WEEKLY) assertEquals(s.weekday, back.weekday)
        }
    }

    @Test fun `the reported rewrites now stay in Advanced`() {
        // Each of these used to be rebuilt by Save into a different schedule.
        for (expr in listOf(
            "0 * * * 1-5",      // became 0 * * * *  (hourly branch ignored the weekday)
            "30 8 * 6 *",       // became 30 8 * * * (daily branch ignored the month)
            "0 9 * * 1-5",      // weekday range
            "0 9 * * 0,6",      // weekday list
            "0 9 * * 7",        // 7 = Sunday: Simple writes 0
            "0 9 1 * *",        // a day of the month
            "05 09 * * *",      // leading zeros: Simple writes "5 9"
            "*/15 * * * *",     // Android Simple has no interval
            "0 0 9 * * *",      // six fields
            "@daily",           // macro
            "0 99 * * *",       // out of range
            "",
        )) {
            assertNull(expr, form.parse(expr))
        }
    }

    @Test fun `whitespace between fields is not meaning`() {
        val s = form.parse("  0  9 *\t* *  ")!!
        assertEquals(CronScheduleForm.DAILY, s.frequency)
        assertEquals(9, s.hour)
        assertEquals(0, s.minute)
    }

    @Test fun `weekly carries its weekday and time instead of Monday 07-00`() {
        val s = form.parse("45 18 * * 5")!!
        assertEquals(CronScheduleForm.WEEKLY, s.frequency)
        assertEquals(18, s.hour)
        assertEquals(45, s.minute)
        assertEquals(5, s.weekday)
        assertEquals("weekly on Friday at 6:45 PM", form.describe(s))
        assertEquals("Sunday", form.weekdayName(0))
        assertEquals((0..6).toList(), CronScheduleForm.WEEKDAYS.map { it.first })
    }

    @Test fun `an out-of-range or blank time builds nothing`() {
        assertNull(form.fromFields(CronScheduleForm.DAILY, "99", "00", 1))
        assertNull(form.fromFields(CronScheduleForm.DAILY, "12", "60", 1))
        assertNull(form.fromFields(CronScheduleForm.DAILY, "", "00", 1))
        assertNull(form.fromFields(CronScheduleForm.WEEKLY, "07", "00", 7))
        assertEquals("0 7 * * 3", form.build(form.fromFields(CronScheduleForm.WEEKLY, "07", "00", 3)!!))
        // Hourly needs no time at all.
        assertEquals("0 * * * *", form.build(form.fromFields(CronScheduleForm.HOURLY, "", "", 1)!!))
    }

    @Test fun `the advanced label never claims a shape it is not`() {
        assertEquals("Cron: 0 * * * 1-5", form.describeExpression("0 * * * 1-5"))
        assertEquals("Every hour", form.describeExpression("0 * * * *"))
        assertEquals("Daily at 7:00 AM", form.describeExpression("0 7 * * *"))
    }

    @Test fun `the editor maps schedules through the form, not its own parser`() {
        val src = source("src/main/java/com/aiblackbox/portal/ui/cron/CronManagerScreen.kt")
        assertTrue(src.contains("CronScheduleForm.parse("))
        assertFalse("the loose single-space parser is gone", src.contains("schedule.split(\" \")"))
        assertFalse("weekly is no longer hardcoded to Monday", src.contains("\"\$m \$h * * 1\""))
        assertFalse("times are not raw ISO substrings", src.contains("substring(5, 16)"))
    }

    private fun source(rel: String): String {
        var dir: File? = File(".").absoluteFile
        while (dir != null) {
            for (candidate in listOf(File(dir, rel), File(dir, "app/$rel"))) {
                if (candidate.isFile) return candidate.readText()
            }
            dir = dir.parentFile
        }
        fail("could not locate $rel from ${File(".").absolutePath}")
        return ""
    }
}
