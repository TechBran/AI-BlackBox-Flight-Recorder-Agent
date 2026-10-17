package com.aiblackbox.portal.data.voice

import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Test

/**
 * The bound on decode-ahead. A chat thread renders ONE player bar per TTS message, so a cache that
 * grew with the thread would hold every envelope the operator ever scrolled past — a five-minute
 * clip's band table alone is ~240 KB.
 *
 * LEAST RECENTLY USED, not least recently ADDED, because the access pattern is replay: the operator
 * scrubs one reply repeatedly while newer ones decode past it, and an insertion-ordered cache would
 * evict the very clip being played.
 */
class EnvelopeCacheTest {

    private fun env(marker: Float) =
        AudioEnvelope.Envelopes(floatArrayOf(marker), emptyArray(), 20)

    @Test
    fun `an envelope comes back by url`() {
        val cache = EnvelopeCache(maxEntries = 4)
        val e = env(1f)
        cache.put("a", e)
        assertSame(e, cache.get("a"))
    }

    @Test
    fun `an unseen url is null`() {
        assertNull(EnvelopeCache(maxEntries = 4).get("nope"))
    }

    @Test
    fun `the cache never grows past maxEntries`() {
        val cache = EnvelopeCache(maxEntries = 2)
        cache.put("a", env(1f))
        cache.put("b", env(2f))
        cache.put("c", env(3f))
        assertEquals(2, cache.size)
        assertNull("the oldest entry was evicted", cache.get("a"))
    }

    @Test
    fun `eviction drops the least recently USED, not the oldest inserted`() {
        val cache = EnvelopeCache(maxEntries = 2)
        val a = env(1f)
        cache.put("a", a)
        cache.put("b", env(2f))
        // Reading "a" is what makes it recent — this is the scrub-one-reply-repeatedly case.
        assertSame(a, cache.get("a"))
        cache.put("c", env(3f))
        assertSame("the clip being replayed survived", a, cache.get("a"))
        assertNull("the untouched one went instead", cache.get("b"))
    }

    @Test
    fun `re-putting a url replaces rather than adding a second entry`() {
        val cache = EnvelopeCache(maxEntries = 2)
        cache.put("a", env(1f))
        val fresh = env(9f)
        cache.put("a", fresh)
        assertEquals(1, cache.size)
        assertSame(fresh, cache.get("a"))
    }

    @Test
    fun `clear empties it`() {
        val cache = EnvelopeCache(maxEntries = 2)
        cache.put("a", env(1f))
        cache.clear()
        assertEquals(0, cache.size)
        assertNull(cache.get("a"))
    }

    @Test
    fun `maxEntries is at least one, so a put is never immediately evicted`() {
        val cache = EnvelopeCache(maxEntries = 0)
        val e = env(1f)
        cache.put("a", e)
        assertSame(e, cache.get("a"))
    }
}
