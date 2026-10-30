package com.aiblackbox.portal.data.wellness

import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.jsonArray
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.Json
import java.time.Instant
import java.time.ZoneOffset
import java.time.format.DateTimeFormatter

/**
 * The `bbx.wellness.ingest/1` payload (spec §3.3, wire contract) and its deterministic JSON.
 *
 *   {"schema", "batch_id", "enrollment_id", "epoch", "window": {type, from, to},
 *    "origins": [{key, package, device_type, model}],
 *    "buckets": [{metric, grain, origin, replace_day, rows}],
 *    "sessions": [{kind, hc_id, origin, start, end, offset_s, stages | subtype + attrs, last_modified}],
 *    "readings": [{metric, hc_id, origin, t, offset_s, v, [v2, attrs], last_modified}],
 *    "deleted_ids": [...]}
 *
 * Field order is fixed and origin keys are assigned in sorted order, so the same records always
 * produce the same bytes: the goldens under `src/test/resources/wellness/` are frozen contracts
 * the box replays through its real ingest route. Times are epoch ms (window ends are ISO-8601
 * UTC); a gap is an absent row, never a zero.
 */
object IngestPayloadBuilder {

    const val SCHEMA = "bbx.wellness.ingest/1"

    /** The origin key for Health Connect's priority-merged activity aggregates. */
    const val ORIGIN_MERGED = "hc_merged"

    enum class Grain(val wire: String) { MIN1("1min"), MIN5("5min"), HOUR("1h") }

    data class Window(val type: String, val fromMs: Long, val toMs: Long)

    /** One metric's rows for one local day; [origin] null = [ORIGIN_MERGED]. */
    data class Bucket(
        val metric: String,
        val grain: Grain,
        val origin: HcOrigin?,
        val replaceDay: String,
        val rows: List<List<Number>>,
    )

    data class Session(
        val kind: String,
        val hcId: String,
        val origin: HcOrigin,
        val startMs: Long,
        val endMs: Long,
        val offsetS: Int,
        val lastModifiedMs: Long,
        /** Sleep: `[start, end, code]` per stage. */
        val stages: List<HcStage>? = null,
        /** Exercise: Health Connect's exercise type. */
        val subtype: Int? = null,
        /** Exercise: distance_m, active_kcal, total_kcal, avg_hr, max_hr (null = not recorded). */
        val attrs: Map<String, Number?>? = null,
    )

    data class Reading(
        val metric: String,
        val hcId: String,
        val origin: HcOrigin,
        val timeMs: Long,
        val offsetS: Int,
        val value: Double,
        val lastModifiedMs: Long,
        /** Blood pressure only: diastolic. */
        val value2: Double? = null,
        /** Blood pressure only: measurement_location, body_position, recording_method. */
        val attrs: Map<String, String>? = null,
    )

    data class Batch(
        val batchId: String,
        val enrollmentId: String,
        val epoch: Int,
        val window: Window,
        val buckets: List<Bucket> = emptyList(),
        val sessions: List<Session> = emptyList(),
        val readings: List<Reading> = emptyList(),
        val deletedIds: List<String> = emptyList(),
    ) {
        val rowCount: Int
            get() = buckets.sumOf { it.rows.size } + sessions.size + readings.size + deletedIds.size

        val isEmpty: Boolean get() = buckets.isEmpty() && sessions.isEmpty() && readings.isEmpty() && deletedIds.isEmpty()
    }

    /** The metric id a reading type is sent under. */
    fun readingMetric(type: HcType): String = when (type) {
        HcType.RESTING_HEART_RATE -> "resting_hr"
        HcType.HRV -> "hrv"
        HcType.SPO2 -> "spo2"
        HcType.RESPIRATORY_RATE -> "respiratory_rate"
        HcType.BLOOD_PRESSURE -> "blood_pressure"
        else -> throw IllegalArgumentException("${type.wire} is not a reading type")
    }

    /** The metric id a bucketed type is sent under. */
    fun bucketMetric(type: HcType): String = when (type) {
        HcType.HEART_RATE -> "heart_rate"
        HcType.STEPS -> "steps"
        HcType.DISTANCE -> "distance"
        HcType.ACTIVE_CALORIES -> "active_calories"
        HcType.TOTAL_CALORIES -> "total_calories"
        else -> throw IllegalArgumentException("${type.wire} is not a bucketed type")
    }

    // ── JSON ───────────────────────────────────────────────────────────────────────────────

    fun toJson(batch: Batch): String = toJsonElement(batch).toString()

    fun toJsonElement(batch: Batch): JsonObject {
        val keys = originKeys(batch)
        fun key(o: HcOrigin?): String = if (o == null) ORIGIN_MERGED else keys.getValue(originIdentity(o))
        return buildJsonObject {
            put("schema", JsonPrimitive(SCHEMA))
            put("batch_id", JsonPrimitive(batch.batchId))
            put("enrollment_id", JsonPrimitive(batch.enrollmentId))
            put("epoch", JsonPrimitive(batch.epoch))
            put("window", buildJsonObject {
                put("type", JsonPrimitive(batch.window.type))
                put("from", JsonPrimitive(iso(batch.window.fromMs)))
                put("to", JsonPrimitive(iso(batch.window.toMs)))
            })
            put("origins", buildJsonArray {
                for ((identity, k) in keys.entries.sortedBy { it.value.removePrefix("o").toInt() }) {
                    add(buildJsonObject {
                        put("key", JsonPrimitive(k))
                        put("package", JsonPrimitive(identity.packageName))
                        put("device_type", identity.deviceType?.let { JsonPrimitive(it) } ?: JsonNull)
                        put("model", identity.model?.let { JsonPrimitive(it) } ?: JsonNull)
                    })
                }
            })
            put("buckets", buildJsonArray {
                for (b in batch.buckets) add(buildJsonObject {
                    put("metric", JsonPrimitive(b.metric))
                    put("grain", JsonPrimitive(b.grain.wire))
                    put("origin", JsonPrimitive(key(b.origin)))
                    put("replace_day", JsonPrimitive(b.replaceDay))
                    put("rows", buildJsonArray {
                        for (row in b.rows) add(buildJsonArray { for (n in row) add(num(n)) })
                    })
                })
            })
            put("sessions", buildJsonArray {
                for (s in batch.sessions) add(buildJsonObject {
                    put("kind", JsonPrimitive(s.kind))
                    put("hc_id", JsonPrimitive(s.hcId))
                    put("origin", JsonPrimitive(key(s.origin)))
                    put("start", JsonPrimitive(s.startMs))
                    put("end", JsonPrimitive(s.endMs))
                    put("offset_s", JsonPrimitive(s.offsetS))
                    if (s.stages != null) put("stages", buildJsonArray {
                        for (st in s.stages) add(buildJsonArray {
                            add(JsonPrimitive(st.startMs)); add(JsonPrimitive(st.endMs)); add(JsonPrimitive(st.code))
                        })
                    })
                    if (s.subtype != null) put("subtype", JsonPrimitive(s.subtype))
                    if (s.attrs != null) put("attrs", buildJsonObject {
                        for ((k, v) in s.attrs.toSortedMap()) put(k, v?.let { num(it) } ?: JsonNull)
                    })
                    put("last_modified", JsonPrimitive(s.lastModifiedMs))
                })
            })
            put("readings", buildJsonArray {
                for (r in batch.readings) add(buildJsonObject {
                    put("metric", JsonPrimitive(r.metric))
                    put("hc_id", JsonPrimitive(r.hcId))
                    put("origin", JsonPrimitive(key(r.origin)))
                    put("t", JsonPrimitive(r.timeMs))
                    put("offset_s", JsonPrimitive(r.offsetS))
                    put("v", num(r.value))
                    if (r.value2 != null) put("v2", num(r.value2))
                    if (r.attrs != null) put("attrs", buildJsonObject {
                        for ((k, v) in r.attrs.toSortedMap()) put(k, JsonPrimitive(v))
                    })
                    put("last_modified", JsonPrimitive(r.lastModifiedMs))
                })
            })
            put("deleted_ids", buildJsonArray { for (id in batch.deletedIds) add(JsonPrimitive(id)) })
        }
    }

    /** The identity an origin key stands for (recording method and manufacturer are not keyed). */
    data class OriginIdentity(val packageName: String, val deviceType: String?, val model: String?)

    fun originIdentity(o: HcOrigin) = OriginIdentity(o.packageName, o.deviceType, o.model)

    /** o1, o2, ... assigned over the batch's distinct origins in sorted order. */
    fun originKeys(batch: Batch): Map<OriginIdentity, String> {
        val all = sortedSetOf(
            compareBy<OriginIdentity>({ it.packageName }, { it.deviceType ?: "" }, { it.model ?: "" }),
        )
        batch.buckets.mapNotNullTo(all) { it.origin?.let(::originIdentity) }
        batch.sessions.mapTo(all) { originIdentity(it.origin) }
        batch.readings.mapTo(all) { originIdentity(it.origin) }
        return all.withIndex().associate { (i, o) -> o to "o${i + 1}" }
    }

    /**
     * Numbers are written in one canonical form: integral values as integers, anything else
     * rounded to 3 decimals. Kotlin, Python and the goldens then agree byte for byte.
     */
    fun num(n: Number): JsonPrimitive = when (n) {
        is Int, is Long, is Short, is Byte -> JsonPrimitive(n.toLong())
        else -> {
            val d = n.toDouble()
            require(d.isFinite()) { "non-finite number in a wellness payload" }
            val rounded = Math.round(d * 1000.0) / 1000.0
            if (rounded == Math.rint(rounded) && kotlin.math.abs(rounded) < 9.0e15) JsonPrimitive(rounded.toLong())
            else JsonPrimitive(rounded)
        }
    }

    private val ISO: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd'T'HH:mm:ss'Z'").withZone(ZoneOffset.UTC)

    fun iso(ms: Long): String = ISO.format(Instant.ofEpochMilli(ms))

    // ── 413: split a batch in two ──────────────────────────────────────────────────────────

    /**
     * Split an encoded batch for a 413. Halves the list of bucket objects, sessions, readings
     * and deletions (a bucket object's rows are never split: each one replaces a whole day),
     * gives each half its own batch id from [newBatchId], and keeps only the origins each half
     * uses. Null when the batch is a single item and cannot be split further (the caller
     * quarantines it).
     */
    fun split(json: String, newBatchId: () -> String): Pair<String, String>? {
        val obj = Json.parseToJsonElement(json).jsonObject
        val items = mutableListOf<Pair<String, JsonElement>>()
        for (section in listOf("buckets", "sessions", "readings", "deleted_ids")) {
            obj[section]?.jsonArray?.forEach { items += section to it }
        }
        if (items.size < 2) return null
        val mid = items.size / 2
        return half(obj, items.subList(0, mid), newBatchId()) to half(obj, items.subList(mid, items.size), newBatchId())
    }

    private fun half(original: JsonObject, items: List<Pair<String, JsonElement>>, batchId: String): String {
        val used = items.mapNotNull { (section, el) ->
            if (section == "deleted_ids") null else el.jsonObject["origin"]?.jsonPrimitive?.content
        }.toSet()
        return buildJsonObject {
            for ((k, v) in original) {
                when (k) {
                    "batch_id" -> put(k, JsonPrimitive(batchId))
                    "origins" -> put(k, JsonArray(v.jsonArray.filter {
                        it.jsonObject["key"]?.jsonPrimitive?.content in used
                    }))
                    "buckets", "sessions", "readings", "deleted_ids" ->
                        put(k, JsonArray(items.filter { it.first == k }.map { it.second }))
                    else -> put(k, v)
                }
            }
        }.toString()
    }
}
