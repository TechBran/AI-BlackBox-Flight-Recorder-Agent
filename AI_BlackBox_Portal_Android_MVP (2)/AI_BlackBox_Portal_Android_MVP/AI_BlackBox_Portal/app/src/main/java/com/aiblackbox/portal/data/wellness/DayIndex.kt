package com.aiblackbox.portal.data.wellness

import java.io.File

/**
 * A values-free index `hc_id -> (type, local day)` (spec §3.2, "Dirty days and deletions").
 *
 * A Health Connect DeletionChange carries only an id. To know which day to re-aggregate (and
 * whether the id was a session or reading the box should delete), the phone remembers, for
 * every record it has seen through the Changes API or read as a session or reading, its type
 * and the local day it started on. No values, no times beyond the day.
 *
 * An id the index has never seen (a heart-rate or step record imported by aggregate during
 * the backfill) resolves to [Resolution.Unknown]: the caller re-aggregates the trailing 30
 * days ([UNKNOWN_REAGGREGATE_DAYS]). Bucketed entries older than [BUCKETED_KEEP_DAYS] are
 * pruned (the monthly reconcile covers the trailing 90 days); session and reading entries are
 * kept so their deletions propagate at any age.
 *
 * Stored as TSV (`<id>\t<type>\t<yyyy-mm-dd>`), loaded lazily, written atomically on [save].
 */
class DayIndex(private val file: File) {

    data class Entry(val type: HcType, val day: String)

    sealed class Resolution {
        data class Known(val id: String, val entry: Entry) : Resolution()
        data class Unknown(val id: String) : Resolution()
    }

    private val lock = Any()
    private var entries: LinkedHashMap<String, Entry>? = null
    private var dirty = false

    fun record(id: String, type: HcType, day: String) = synchronized(lock) {
        if (id.isBlank() || '\t' in id || '\n' in id) return
        val map = loaded()
        val next = Entry(type, day)
        if (map[id] != next) {
            map[id] = next
            dirty = true
        }
    }

    fun lookup(id: String): Entry? = synchronized(lock) { loaded()[id] }

    /** Resolve a deletion and forget the id. */
    fun resolveDeletion(id: String): Resolution = synchronized(lock) {
        val map = loaded()
        val entry = map.remove(id)
        if (entry != null) {
            dirty = true
            Resolution.Known(id, entry)
        } else {
            Resolution.Unknown(id)
        }
    }

    val size: Int get() = synchronized(lock) { loaded().size }

    /** Drop bucketed-type entries whose day is before [cutoffDay] (ISO dates sort as strings). */
    fun pruneBucketed(cutoffDay: String) = synchronized(lock) {
        val map = loaded()
        val it = map.entries.iterator()
        while (it.hasNext()) {
            val e = it.next().value
            if (e.type.shape == HcShape.BUCKETED && e.day < cutoffDay) {
                it.remove()
                dirty = true
            }
        }
    }

    fun save() = synchronized(lock) {
        val map = entries ?: return
        if (!dirty) return
        val text = buildString {
            for ((id, e) in map) append(id).append('\t').append(e.type.wire).append('\t').append(e.day).append('\n')
        }
        WellnessAtomic.writeText(file, text)
        dirty = false
    }

    fun clear() = synchronized(lock) {
        entries = LinkedHashMap()
        dirty = false
        file.delete()
        Unit
    }

    private fun loaded(): LinkedHashMap<String, Entry> {
        entries?.let { return it }
        val map = LinkedHashMap<String, Entry>()
        WellnessAtomic.readTextOrNull(file)?.lineSequence()?.forEach { line ->
            val parts = line.split('\t')
            if (parts.size != 3) return@forEach
            val type = HcType.fromWire(parts[1]) ?: return@forEach
            if (!DAY_RE.matches(parts[2])) return@forEach
            map[parts[0]] = Entry(type, parts[2])
        }
        entries = map
        return map
    }

    companion object {
        const val UNKNOWN_REAGGREGATE_DAYS = 30
        const val RECONCILE_DAYS = 90
        const val BUCKETED_KEEP_DAYS = 120
        private val DAY_RE = Regex("\\d{4}-\\d{2}-\\d{2}")
    }
}
