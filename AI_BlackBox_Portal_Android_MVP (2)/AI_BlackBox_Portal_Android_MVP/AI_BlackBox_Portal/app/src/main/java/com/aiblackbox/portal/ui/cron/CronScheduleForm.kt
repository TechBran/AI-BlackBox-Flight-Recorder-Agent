package com.aiblackbox.portal.ui.cron

/**
 * The cron editor's Simple schedule: parse, build and describe, with no Android
 * dependencies so the JVM tests cover it (A12-voice-cron-01, -15).
 *
 * Opening a job used to map its expression onto the Simple form loosely: the hourly
 * branch ignored the day, month and weekday fields, the daily branch ignored the month,
 * anything that did not split into five fields on single spaces left the form at
 * "daily 07:00", and weekly was hardcoded to Monday. Saving then rebuilt the schedule
 * from the form, so editing only a job's name changed when it ran ('0 * * * 1-5'
 * became '0 * * * *'). [parse] now maps an expression to Simple only when [build]
 * gives back exactly the same expression; everything else stays in Advanced, verbatim.
 *
 * Weekdays are standard cron numbers, 0 = Sunday .. 6 = Saturday, as the box reads them.
 */
object CronScheduleForm {
    const val HOURLY = "hourly"
    const val DAILY = "daily"
    const val WEEKLY = "weekly"

    /** The weekday picker, in standard cron numbering (0 = Sunday). */
    val WEEKDAYS: List<Pair<Int, String>> = listOf(
        0 to "Sunday", 1 to "Monday", 2 to "Tuesday", 3 to "Wednesday",
        4 to "Thursday", 5 to "Friday", 6 to "Saturday",
    )

    /** A Simple schedule. [hour]/[minute] apply to daily and weekly, [weekday] to weekly. */
    data class Simple(
        val frequency: String,
        val hour: Int = 7,
        val minute: Int = 0,
        val weekday: Int = 1,
    )

    /** The hour typed in the form (0-23), or null. */
    fun hourOf(text: String): Int? = text.trim().toIntOrNull()?.takeIf { it in 0..23 }

    /** The minute typed in the form (0-59), or null. */
    fun minuteOf(text: String): Int? = text.trim().toIntOrNull()?.takeIf { it in 0..59 }

    /** The form's fields as a Simple schedule, or null while the time is not valid. */
    fun fromFields(frequency: String, hourText: String, minuteText: String, weekday: Int): Simple? {
        if (frequency == HOURLY) return Simple(HOURLY)
        val hour = hourOf(hourText) ?: return null
        val minute = minuteOf(minuteText) ?: return null
        return Simple(frequency, hour, minute, weekday).takeIf { build(it) != null }
    }

    /** The cron expression for [s], or null when it does not describe one. */
    fun build(s: Simple): String? {
        val timeOk = s.hour in 0..23 && s.minute in 0..59
        return when (s.frequency) {
            HOURLY -> "0 * * * *"
            DAILY -> if (timeOk) "${s.minute} ${s.hour} * * *" else null
            WEEKLY -> if (timeOk && s.weekday in 0..6) "${s.minute} ${s.hour} * * ${s.weekday}" else null
            else -> null
        }
    }

    /**
     * The Simple form for [expression], or null when Simple cannot rebuild it exactly
     * (a weekday range or list, a month, 7 for Sunday, leading zeros, six fields, a
     * macro): the editor then opens it in Advanced and saves it unchanged.
     */
    fun parse(expression: String): Simple? {
        val parts = expression.trim().split(Regex("\\s+"))
        if (parts.size != 5) return null
        val (min, hour, dom, month, dow) = parts
        if (dom != "*" || month != "*") return null
        val candidate = when {
            min == "0" && hour == "*" && dow == "*" -> Simple(HOURLY)
            dow == "*" -> Simple(DAILY, hour.toIntOrNull() ?: return null, min.toIntOrNull() ?: return null)
            dow.length == 1 && dow[0].isDigit() -> Simple(
                WEEKLY, hour.toIntOrNull() ?: return null, min.toIntOrNull() ?: return null, dow.toInt(),
            )
            else -> return null
        }
        return candidate.takeIf { build(it) == parts.joinToString(" ") }
    }

    /** "every hour" / "daily at 7:00 AM" / "weekly on Monday at 7:00 AM". */
    fun describe(s: Simple): String {
        val time = timeLabel(s.hour, s.minute)
        return when (s.frequency) {
            HOURLY -> "every hour"
            DAILY -> "daily at $time"
            WEEKLY -> "weekly on ${weekdayName(s.weekday)} at $time"
            else -> s.frequency
        }
    }

    /** A label for an Advanced expression: the Simple wording when it is one, else the cron. */
    fun describeExpression(expression: String): String {
        val simple = parse(expression) ?: return "Cron: ${expression.trim()}"
        return describe(simple).replaceFirstChar { it.uppercase() }
    }

    fun weekdayName(day: Int): String = WEEKDAYS.firstOrNull { it.first == day }?.second ?: "day $day"

    private fun timeLabel(hour: Int, minute: Int): String {
        val ampm = if (hour >= 12) "PM" else "AM"
        val h12 = if (hour % 12 == 0) 12 else hour % 12
        return "$h12:${minute.toString().padStart(2, '0')} $ampm"
    }
}
