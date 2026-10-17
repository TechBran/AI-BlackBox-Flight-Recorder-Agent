package com.aiblackbox.portal.data.voice

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.ExperimentalCoroutinesApi
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.async
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertSame
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Decode-ahead has to be BOUNDED before it can ship. One player bar per TTS message means a long
 * thread asks for dozens of whole-file MediaCodec decodes as it scrolls; running them all is worse
 * than not prefetching at all, because each one holds a hardware decoder while it runs.
 *
 * Three bounds, tested here: the cache (never decode the same clip twice), the permit count (never
 * more than [EnvelopePrefetcher.MAX_CONCURRENT] decoders alive), and the queue (a request that has
 * scrolled far off screen is dropped rather than queued forever).
 */
@OptIn(ExperimentalCoroutinesApi::class)
class EnvelopePrefetcherTest {

    /**
     * A decoder that never finishes until the test says so, and that hands the test back the
     * cooperative `isActive` probe it was given — which is the only channel a BLOCKING MediaCodec
     * decode can be stopped through.
     */
    private class FakeDecoder {
        val started = ArrayList<String>()
        val probes = HashMap<String, () -> Boolean>()
        private val gates = HashMap<String, CompletableDeferred<Unit>>()
        var concurrent = 0
        var peakConcurrent = 0

        fun gate(url: String) = synchronized(gates) { gates.getOrPut(url) { CompletableDeferred() } }
        fun release(url: String) { gate(url).complete(Unit) }

        suspend fun decode(url: String, isActive: () -> Boolean): AudioEnvelope.Envelopes? {
            started += url
            probes[url] = isActive
            concurrent++
            peakConcurrent = maxOf(peakConcurrent, concurrent)
            try {
                gate(url).await()
                if (!isActive()) return null
                return AudioEnvelope.Envelopes(floatArrayOf(1f), emptyArray(), 20)
            } finally {
                concurrent--
            }
        }
    }

    private val supervisor = SupervisorJob()
    private var hostScope: CoroutineScope? = null

    /**
     * The prefetcher's host scope, on the test scheduler.
     *
     * Deliberately NOT `runTest`'s `backgroundScope`: work launched there is not executed by
     * `advanceUntilIdle`, so every decode below would silently never start and every assertion
     * would pass or fail for the wrong reason. A supervisor of our own also keeps a decode that is
     * still suspended at the end of a test from hanging the runner.
     */
    private fun TestScope.scope(): CoroutineScope = hostScope
        ?: CoroutineScope(StandardTestDispatcher(testScheduler) + supervisor).also { hostScope = it }

    @After
    fun tearDown() {
        supervisor.cancel()
    }

    @Test
    fun `a decoded envelope lands in the cache, so a second request never decodes again`() = runTest {
        val fake = FakeDecoder()
        val cache = EnvelopeCache(maxEntries = 4)
        val p = EnvelopePrefetcher(scope(), cache, decode = fake::decode)

        p.prefetch("a")
        advanceUntilIdle()
        fake.release("a")
        advanceUntilIdle()

        assertNotNull("the decode was cached", cache.get("a"))
        p.prefetch("a")
        advanceUntilIdle()
        assertEquals(listOf("a"), fake.started)
    }

    @Test
    fun `two requests for one url share a single decode`() = runTest {
        val fake = FakeDecoder()
        val p = EnvelopePrefetcher(scope(), EnvelopeCache(4), decode = fake::decode)

        p.prefetch("a")
        p.prefetch("a")
        advanceUntilIdle()

        assertEquals(listOf("a"), fake.started)
    }

    @Test
    fun `awaiting a url that is already in flight returns that decode's result`() = runTest {
        val fake = FakeDecoder()
        val cache = EnvelopeCache(4)
        val p = EnvelopePrefetcher(scope(), cache, decode = fake::decode)

        p.prefetch("a")
        advanceUntilIdle()
        val awaited = scope().async { p.await("a") }
        advanceUntilIdle()
        fake.release("a")
        advanceUntilIdle()

        assertEquals(listOf("a"), fake.started)
        assertSame(cache.get("a"), awaited.await())
    }

    @Test
    fun `no more than MAX_CONCURRENT decoders are ever alive at once`() = runTest {
        val fake = FakeDecoder()
        val p = EnvelopePrefetcher(
            scope(), EnvelopeCache(8),
            maxConcurrent = 2, maxPending = 8, decode = fake::decode,
        )

        listOf("a", "b", "c", "d").forEach { p.prefetch(it) }
        advanceUntilIdle()
        assertEquals(listOf("a", "b"), fake.started)
        assertEquals(2, fake.peakConcurrent)

        fake.release("a")
        fake.release("b")
        advanceUntilIdle()
        assertEquals(listOf("a", "b", "c", "d"), fake.started)
        assertEquals("the permit count held for the whole run", 2, fake.peakConcurrent)
    }

    @Test
    fun `a request that overflows the queue is dropped, oldest waiter first`() = runTest {
        val fake = FakeDecoder()
        val p = EnvelopePrefetcher(
            scope(), EnvelopeCache(8),
            maxConcurrent = 1, maxPending = 2, decode = fake::decode,
        )

        p.prefetch("a")          // runs
        advanceUntilIdle()
        p.prefetch("b")          // queued
        p.prefetch("c")          // queue full -> "b" (the oldest waiter) is dropped, not "a"
        advanceUntilIdle()
        assertEquals(listOf("a"), fake.started)

        fake.release("a")
        advanceUntilIdle()
        fake.release("c")
        advanceUntilIdle()

        assertEquals("the dropped request never decoded", listOf("a", "c"), fake.started)
    }

    @Test
    fun `cancelling a running decode tells it to stop through the cooperative probe`() = runTest {
        val fake = FakeDecoder()
        val cache = EnvelopeCache(4)
        val p = EnvelopePrefetcher(scope(), cache, decode = fake::decode)

        p.prefetch("a")
        advanceUntilIdle()
        assertTrue("still wanted", fake.probes.getValue("a")())

        p.cancel("a")
        assertFalse("a blocking decode is stopped by this and nothing else", fake.probes.getValue("a")())

        fake.release("a")
        advanceUntilIdle()
        assertNull("an abandoned decode must not populate the cache", cache.get("a"))
    }

    @Test
    fun `cancelAll drops every in-flight decode`() = runTest {
        val fake = FakeDecoder()
        val p = EnvelopePrefetcher(
            scope(), EnvelopeCache(8),
            maxConcurrent = 2, maxPending = 8, decode = fake::decode,
        )

        p.prefetch("a")
        p.prefetch("b")
        advanceUntilIdle()
        p.cancelAll()

        assertFalse(fake.probes.getValue("a")())
        assertFalse(fake.probes.getValue("b")())
    }

    @Test
    fun `a caller waiting on a dropped decode is not cancelled along with it`() = runTest {
        // Decodes are SHARED, so a decode can be dropped for reasons that have nothing to do with
        // the coroutine waiting on it (queue overflow, a newer clip). Letting that cancellation
        // through would tear down the playback manager's job for someone else's eviction.
        val fake = FakeDecoder()
        val p = EnvelopePrefetcher(scope(), EnvelopeCache(4), decode = fake::decode)

        var returned = false
        val waiter = scope().async { p.await("a").also { returned = true } }
        advanceUntilIdle()

        p.cancel("a")
        fake.release("a")
        advanceUntilIdle()

        assertTrue("the waiter was cancelled with the decode", returned)
        assertNull(waiter.await())
    }

    @Test
    fun `a decoder that throws is contained rather than killing the scope`() = runTest {
        val p = EnvelopePrefetcher(scope(), EnvelopeCache(4)) { _, _ ->
            throw IllegalStateException("codec exploded")
        }
        p.prefetch("a")
        advanceUntilIdle()
        assertNull(p.await("a"))
    }
}
