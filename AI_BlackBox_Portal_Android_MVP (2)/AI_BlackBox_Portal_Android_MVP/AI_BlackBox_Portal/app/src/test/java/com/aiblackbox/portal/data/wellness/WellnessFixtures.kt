package com.aiblackbox.portal.data.wellness

import java.io.File
import java.nio.file.Files
import java.time.LocalDateTime
import java.time.ZoneId

/**
 * Synthetic records for the wellness JVM tests (operator `wellness-fixture`, never a real
 * person). Times are local to [ZONE] on fixed days so every test, and the frozen goldens,
 * are deterministic. Blood pressure uses the leak-scan sentinel 118/79.
 */
object WellnessFixtures {
    const val OPERATOR = "wellness-fixture"
    val ZONE: ZoneId = ZoneId.of("America/New_York")
    const val OFFSET_EST = -18000

    val WATCH = HcOrigin(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE, "WATCH", "Samsung", "SM-R960", HcOrigin.RECORDING_AUTOMATIC)
    val WATCH_ACTIVE = WATCH.copy(recordingMethod = HcOrigin.RECORDING_ACTIVE)
    val PHONE = HcOrigin(WellnessCatalog.SAMSUNG_HEALTH_PACKAGE, "PHONE", "samsung", "SM-TEST0", HcOrigin.RECORDING_AUTOMATIC)
    val CUFF = HcOrigin("com.example.cuffapp", null, null, null, HcOrigin.RECORDING_MANUAL)

    /** Epoch ms of a local wall-clock time on 2026-11-[day] in [ZONE]. */
    fun at(day: Int, hour: Int, minute: Int = 0, month: Int = 11): Long =
        LocalDateTime.of(2026, month, day, hour, minute).atZone(ZONE).toInstant().toEpochMilli()

    fun meta(id: String, origin: HcOrigin = WATCH, lastModified: Long = at(3, 8)) = HcMeta(id, origin, lastModified)

    /** A night from 23:10 on day-1 to 06:40 on [day] with light / deep / REM / awake stages. */
    fun sleepNight(id: String, day: Int): HcRecord.Sleep {
        val s = at(day - 1, 23, 10)
        val e = at(day, 6, 40)
        val stages = listOf(
            HcStage(s, at(day, 0, 30), 4),
            HcStage(at(day, 0, 30), at(day, 1, 40), 5),
            HcStage(at(day, 1, 40), at(day, 2, 0), 1),
            HcStage(at(day, 2, 0), at(day, 4, 30), 4),
            HcStage(at(day, 4, 30), e, 6),
        )
        return HcRecord.Sleep(meta(id, lastModified = at(day, 7)), s, e, OFFSET_EST, stages)
    }

    fun run(id: String, day: Int): HcRecord.Exercise =
        HcRecord.Exercise(meta(id, WATCH_ACTIVE, at(day, 18, 45)), at(day, 18, 0), at(day, 18, 30), OFFSET_EST, 56)

    fun bp(id: String, day: Int, hour: Int, sys: Double = 118.0, dia: Double = 79.0) = HcRecord.Reading(
        HcType.BLOOD_PRESSURE, meta(id, CUFF, at(day, hour, 5)), at(day, hour, 2), OFFSET_EST, sys, dia,
        linkedMapOf("measurement_location" to "left_upper_arm", "body_position" to "sitting_down"),
    )

    fun spo2(id: String, day: Int, hour: Int, v: Double) =
        HcRecord.Reading(HcType.SPO2, meta(id), at(day, hour), OFFSET_EST, v)

    fun steps(id: String, start: Long, end: Long, origin: HcOrigin = PHONE) =
        HcRecord.Interval(HcType.STEPS, meta(id, origin), start, end, OFFSET_EST)

    /** Heart-rate samples every [everyS] seconds over [startMs, endMs) around a gentle curve. */
    fun hrSeries(startMs: Long, endMs: Long, everyS: Int, base: Double): List<Pair<Long, Double>> {
        val out = mutableListOf<Pair<Long, Double>>()
        var t = startMs
        var i = 0
        while (t < endMs) {
            out += t to (base + (i % 7) - 3)
            t += everyS * 1000L
            i++
        }
        return out
    }

    fun tempDir(prefix: String = "wellness"): File = Files.createTempDirectory(prefix).toFile()

    /** A fixed 32-hex batch id sequence for deterministic payloads. */
    fun ids(prefix: String = "b"): () -> String {
        var n = 0
        return { prefix + "%0${32 - prefix.length}d".format(++n) }
    }
}
