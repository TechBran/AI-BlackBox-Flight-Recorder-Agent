package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.intOrNull
import kotlinx.serialization.json.jsonObject

/**
 * "Show numbers" (spec §6.1 "Chat"): what a masked turn's health result said, rebuilt from the
 * delivered envelope the box keeps (`GET /wellness/ref/{ref}`, signed: `{ref, ts, tool,
 * categories, payload}`, the payload being the wellness_* tool's envelope, api.py). PURE; the
 * sheet only renders it. Built from the envelope's own display strings ("6 h 49 min", "52 bpm",
 * "7,213 steps"); only the history table carries bare values, shown with their unit.
 *
 * Never persisted and never logged: the sheet holds it while it is open (WellnessRefSheetView).
 */
data class WellnessRefSheet(
    val ref: String,
    val title: String,
    /** The envelope's `as_of` (ISO-8601), formatted by the view with [WellnessChatRules.asOfLabel]. */
    val asOf: String?,
    val stale: Boolean,
    val rows: List<Row>,
    /** "Sleep: worn all night (99%)"; one line per metric that reports coverage. */
    val coverage: List<String>,
    /** Gaps (a metric not recorded, and why), troubles and the envelope's notes. */
    val notes: List<String>,
) {
    data class Row(val label: String, val value: String, val detail: String? = null)

    companion object {
        private val JSON = Json { ignoreUnknownKeys = true; isLenient = true }
        private const val MAX_ROWS = 120

        /** The sheet for one `/wellness/ref` answer body, or null when it is not one. */
        fun parse(body: String): WellnessRefSheet? {
            val root = runCatching { JSON.parseToJsonElement(body).jsonObject }.getOrNull() ?: return null
            val ref = root.str("ref") ?: return null
            val env = root["payload"] as? JsonObject ?: return null
            val tool = root.str("tool") ?: env.str("tool") ?: ""
            if (env["ok"]?.let { (it as? JsonPrimitive)?.contentOrNull } == "false") {
                return WellnessRefSheet(ref, titleFor(tool, env), env.str("as_of"), env.bool("stale"), emptyList(),
                    emptyList(), listOf("This result had no health numbers."))
            }
            val rows = when (tool) {
                "wellness_summary" -> summaryRows(env)
                "wellness_series" -> seriesRows(env)
                "wellness_sessions" -> sessionRows(env.arr("sessions"))
                "wellness_history" -> historyRows(env)
                else -> emptyList()
            }.take(MAX_ROWS)
            return WellnessRefSheet(
                ref = ref,
                title = titleFor(tool, env),
                asOf = env.str("as_of"),
                stale = env.bool("stale"),
                rows = rows,
                coverage = coverageLines(env),
                notes = noteLines(env, rowsShown = rows.isNotEmpty()),
            )
        }

        private fun titleFor(tool: String, env: JsonObject): String {
            val period = (env["period"] as? JsonObject)?.str("label")
            val window = env.str("window")
            return when (tool) {
                "wellness_summary" -> period?.replaceFirstChar { it.uppercase() } ?: "Summary"
                "wellness_series" -> listOfNotNull(metricName(env.str("metric")), window).joinToString(", ")
                "wellness_sessions" -> listOfNotNull(
                    when (env.str("kind")) { "sleep" -> "Sleep sessions"; "exercise" -> "Workouts"; else -> "Sessions" },
                    window,
                ).joinToString(", ")
                "wellness_history" -> listOfNotNull("Day by day", listOfNotNull(env.str("start"), env.str("end"))
                    .takeIf { it.size == 2 }?.joinToString(" to ")).joinToString(", ")
                "wellness_sources" -> "Sources"
                else -> "Health data"
            }
        }

        private fun summaryRows(env: JsonObject): List<Row> {
            val out = mutableListOf<Row>()
            for (m in env.arr("metrics")) {
                val label = m.str("label") ?: metricName(m.str("metric")) ?: continue
                val value = m.str("display") ?: "not recorded"
                val vs = m["vs_usual"] as? JsonObject
                val detail = listOfNotNull(vs?.str("label"), vs?.str("delta_display")).joinToString(", ").ifEmpty { null }
                out += Row(label, value, detail)
            }
            out += sessionRows(env.arr("sessions"))
            return out
        }

        private fun seriesRows(env: JsonObject): List<Row> = env.arr("points").mapNotNull { p ->
            val t = p.str("t") ?: return@mapNotNull null
            val pct = p["coverage_pct"]?.let { (it as? JsonPrimitive)?.intOrNull }
            Row(t, p.str("display") ?: "not recorded", pct?.takeIf { it < 100 }?.let { "coverage $it%" })
        }

        private fun sessionRows(sessions: List<JsonObject>): List<Row> = sessions.map { s ->
            val kind = s.str("type") ?: s.str("kind") ?: "session"
            val start = s.str("start")?.let { it.replace('T', ' ').take(16) }
            val parts = listOfNotNull(
                s.str("distance"),
                s.str("avg_hr_display")?.let { "average heart rate $it" },
                s["hr_coverage_pct"]?.let { (it as? JsonPrimitive)?.intOrNull }?.takeIf { it < 100 }?.let { "heart-rate coverage $it%" },
                s.str("calories"),
                s.str("source"),
            )
            Row(listOfNotNull(kind.replaceFirstChar { it.uppercase() }, start).joinToString(", "),
                s.str("duration") ?: "", parts.joinToString(" · ").ifEmpty { null })
        }

        /** The history table: one row per day, each recorded cell with its unit (the envelope carries no display strings here). */
        private fun historyRows(env: JsonObject): List<Row> {
            val cols = (env["columns"] as? JsonArray)?.mapNotNull { (it as? JsonPrimitive)?.contentOrNull } ?: return emptyList()
            val units = env["units"] as? JsonObject
            val rows = env["rows"] as? JsonArray ?: return emptyList()
            return rows.mapNotNull { r ->
                val cells = (r as? JsonArray) ?: return@mapNotNull null
                val day = (cells.firstOrNull() as? JsonPrimitive)?.contentOrNull ?: return@mapNotNull null
                val shown = cols.drop(1).zip(cells.drop(1)).mapNotNull { (col, v) -> historyCell(col, v, units?.str(col)) }
                Row(day, if (shown.isEmpty()) "not recorded" else shown.joinToString(" · "))
            }
        }

        private val HISTORY_SKIP = setOf("night_worn", "day_wear_h")

        private fun historyCell(col: String, v: JsonElement, unit: String?): String? {
            if (col in HISTORY_SKIP || v is JsonNull) return null
            val p = v as? JsonPrimitive ?: return null
            val name = metricName(col) ?: col.replace('_', ' ')
            if (unit == "clock" || p.isString) return "$name ${p.content}"
            val n = p.doubleOrNull ?: return null
            val text = when (unit) {
                "min" -> durationText(n)
                "m" -> if (n >= 1000) "${trim(n / 1000.0, 1)} km" else "${trim(n, 0)} m"
                null, "" -> trim(n, 1)
                "steps" -> "%,d steps".format(java.util.Locale.ROOT, Math.round(n))
                else -> "${trim(n, 1)} $unit"
            }
            return "$name $text"
        }

        private fun durationText(minutes: Double): String {
            val total = Math.round(minutes)
            val h = total / 60
            val m = total % 60
            return if (h > 0) "$h h %02d min".format(java.util.Locale.ROOT, m) else "$m min"
        }

        private fun trim(n: Double, decimals: Int): String {
            val rounded = if (decimals == 0) Math.round(n).toDouble() else Math.round(n * 10.0) / 10.0
            return if (rounded == Math.floor(rounded)) rounded.toLong().toString() else rounded.toString()
        }

        private fun coverageLines(env: JsonObject): List<String> = env.arr("metrics").mapNotNull { m ->
            val cov = m["coverage"] as? JsonObject ?: return@mapNotNull null
            val label = cov.str("label") ?: return@mapNotNull null
            val pct = cov["pct"]?.let { (it as? JsonPrimitive)?.intOrNull }
            val name = m.str("label") ?: metricName(m.str("metric")) ?: return@mapNotNull null
            "$name: $label" + (pct?.let { " ($it%)" } ?: "")
        }.distinct()

        private fun noteLines(env: JsonObject, rowsShown: Boolean): List<String> {
            val out = mutableListOf<String>()
            for (g in env.arr("gaps")) {
                val name = metricName(g.str("metric")) ?: g.str("metric") ?: continue
                out += "Not recorded: $name" + (g.str("day")?.let { " on $it" } ?: "") + (g.str("reason")?.let { " ($it)" } ?: "")
            }
            for (t in env.arr("troubles")) t.str("title")?.let { out += it }
            (env["notes"] as? JsonArray)?.forEach { n -> (n as? JsonPrimitive)?.contentOrNull?.let { out += it } }
            if (!rowsShown && out.isEmpty()) out += "This result had no health numbers."
            return out.distinct()
        }

        /** The catalog's metric names, for the few places the envelope gives an id only. */
        private val METRIC_NAMES = mapOf(
            "sleep_duration" to "Sleep", "sleep_stages" to "Sleep stages", "sleep_onset" to "Asleep",
            "sleep_wake" to "Woke", "sleeping_hr_median" to "Sleeping heart rate",
            "sleeping_hr_p10" to "Sleeping heart rate (low)", "resting_hr" to "Resting heart rate",
            "heart_rate" to "Heart rate", "hrv" to "Heart rate variability", "spo2" to "Blood oxygen",
            "respiratory_rate" to "Breathing rate", "steps" to "Steps", "distance" to "Distance",
            "active_calories" to "Active calories", "exercise" to "Exercise", "blood_pressure" to "Blood pressure",
        )

        private fun metricName(id: String?): String? = id?.let { METRIC_NAMES[it] }

        private fun JsonObject.str(key: String): String? =
            (this[key] as? JsonPrimitive)?.takeIf { it.isString }?.contentOrNull?.takeIf { it.isNotBlank() }

        private fun JsonObject.bool(key: String): Boolean = (this[key] as? JsonPrimitive)?.contentOrNull == "true"

        private fun JsonObject.arr(key: String): List<JsonObject> =
            (this[key] as? JsonArray)?.mapNotNull { it as? JsonObject }.orEmpty()
    }
}

/** What Show numbers ends in: the sheets (one per ref), or "Not available" with no detail. */
sealed class ShowNumbersResult {
    data class Loaded(val sheets: List<WellnessRefSheet>) : ShowNumbersResult()
    data object NotAvailable : ShowNumbersResult()

    companion object {
        /**
         * The result for the box's answers, one (status, body) per ref, in order. Any ref that
         * reads is shown; all refused (401 / 403 / 404 / no answer) is "Not available", and
         * never says why (rule W17: no hints).
         */
        fun of(replies: List<Pair<Int, String>>): ShowNumbersResult {
            val sheets = replies.filter { it.first == 200 }.mapNotNull { WellnessRefSheet.parse(it.second) }
            return if (sheets.isEmpty()) NotAvailable else Loaded(sheets)
        }
    }
}
