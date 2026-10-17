package com.aiblackbox.portal.data.voice

/**
 * Decoded envelopes, kept by url, bounded.
 *
 * WHY BOUNDED. A chat thread renders one [com.aiblackbox.portal.ui.components.AudioPlayerBar] per
 * TTS message and every one of them warms its envelope, so "remember what we decoded" without a
 * ceiling means holding every clip the operator ever scrolled past. A five-minute reply's band
 * table is ~240 KB on its own.
 *
 * WHY LEAST RECENTLY *USED*. The access pattern is replay, not a stream: the operator scrubs one
 * reply repeatedly while newer bars decode past it. Insertion order would evict the clip currently
 * under the playhead.
 *
 * Synchronized because the writers are decode coroutines on [kotlinx.coroutines.Dispatchers.IO] and
 * the readers are the playback manager and the composition — the one place in this path where more
 * than one thread genuinely touches the same structure.
 */
internal class EnvelopeCache(maxEntries: Int = DEFAULT_MAX_ENTRIES) {

    private val maxEntries = maxEntries.coerceAtLeast(1)

    // accessOrder = true is the LRU itself; removeEldestEntry is the ceiling.
    private val entries = object : LinkedHashMap<String, AudioEnvelope.Envelopes>(16, 0.75f, true) {
        override fun removeEldestEntry(
            eldest: MutableMap.MutableEntry<String, AudioEnvelope.Envelopes>?,
        ): Boolean = size > this@EnvelopeCache.maxEntries
    }

    val size: Int get() = synchronized(entries) { entries.size }

    /** The envelope for [url], counting as a USE — this is what keeps the playing clip resident. */
    fun get(url: String): AudioEnvelope.Envelopes? = synchronized(entries) { entries[url] }

    fun put(url: String, envelopes: AudioEnvelope.Envelopes) {
        synchronized(entries) { entries[url] = envelopes }
    }

    fun clear() {
        synchronized(entries) { entries.clear() }
    }

    companion object {
        /**
         * Roughly a screen of player bars plus the one being replayed. Entries rather than bytes:
         * the envelopes are small and uniform in shape, and a byte budget would need every clip's
         * length before it could decide anything.
         */
        const val DEFAULT_MAX_ENTRIES = 8
    }
}
