package com.aiblackbox.portal.data.voice

import android.util.Log
import kotlinx.coroutines.CancellationException
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Deferred
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.async
import kotlinx.coroutines.ensureActive
import kotlinx.coroutines.sync.Semaphore
import kotlinx.coroutines.sync.withPermit
import kotlinx.coroutines.withContext
import kotlin.coroutines.coroutineContext

/**
 * Decode-ahead for the player bar, with the three bounds that make it safe to run from a scrolling
 * list.
 *
 * THE REGRESSION THIS AVOIDS. Every TTS message in a thread renders its own player bar, and every
 * bar warms its envelope on composition. Left unbounded, scrolling a long thread starts dozens of
 * whole-file MediaCodec decodes at once — each one holding a hardware decoder, all of them
 * competing for the same IO — which is strictly worse than decoding lazily at tap time. So:
 *
 *  1. [cache] — a url is decoded once, ever (within the LRU's reach).
 *  2. [maxConcurrent] permits — no more decoders are alive at one time than that.
 *  3. [maxPending] — the queue is a WINDOW, not a backlog: request number `maxPending + 1` drops the
 *     oldest waiter, which is the bar furthest off screen and the one least likely to be tapped.
 *     Running decodes are never dropped to make room; they already hold their permit.
 *
 * Cancellation is COOPERATIVE and cannot be anything else: [AudioEnvelope.decode] is a blocking
 * pump, so cancelling its coroutine leaves it running inside MediaCodec. Every entry carries a
 * `live` flag which is what the decode actually polls; [cancel] flips it AND cancels the coroutine,
 * so a queued decode never starts and a running one returns through its `finally`.
 *
 * @param decode injectable so the whole policy above is unit-testable without a codec.
 */
internal class EnvelopePrefetcher(
    private val scope: CoroutineScope,
    private val cache: EnvelopeCache = EnvelopeCache(),
    private val maxConcurrent: Int = MAX_CONCURRENT,
    private val maxPending: Int = MAX_PENDING,
    private val decode: suspend (url: String, isActive: () -> Boolean) -> AudioEnvelope.Envelopes? =
        { url, isActive -> withContext(Dispatchers.IO) { AudioEnvelope.decode(url, isActive = isActive) } },
) {

    private class Entry(val url: String) {
        @Volatile var live = true
        @Volatile var running = false
        lateinit var job: Deferred<AudioEnvelope.Envelopes?>
    }

    private val permits = Semaphore(maxConcurrent.coerceAtLeast(1))

    // Insertion-ordered: the head is the oldest request, which is what overflow drops.
    private val inFlight = LinkedHashMap<String, Entry>()

    /** The envelope for [url] if it is already decoded. Never blocks, never starts anything. */
    fun cached(url: String): AudioEnvelope.Envelopes? = cache.get(url)

    /** Warm [url] in the background. Idempotent, deduplicated, and subject to all three bounds. */
    fun prefetch(url: String) {
        startOrJoin(url)
    }

    /**
     * The envelope for [url], decoding it (or joining the decode already running) if needed. Null
     * when the decode failed or was abandoned.
     */
    suspend fun await(url: String): AudioEnvelope.Envelopes? {
        cache.get(url)?.let { return it }
        val job = startOrJoin(url) ?: return cache.get(url)
        return try {
            job.await()
        } catch (e: CancellationException) {
            // Two very different cancellations arrive here. If THIS caller was cancelled, the
            // exception is its own and must propagate — swallowing it would let a cancelled
            // coroutine keep running. If the caller is still active, the shared decode was simply
            // dropped (queue overflow, or a newer clip), which is not this caller's failure.
            coroutineContext.ensureActive()
            cache.get(url)
        }
    }

    /** Abandon [url]'s decode: the cooperative flag first, then the coroutine. */
    fun cancel(url: String) {
        val entry = synchronized(inFlight) { inFlight.remove(url) } ?: return
        drop(entry)
    }

    /** Abandon every in-flight decode. The cache is kept — what is decoded stays decoded. */
    fun cancelAll() {
        val entries = synchronized(inFlight) {
            val all = inFlight.values.toList()
            inFlight.clear()
            all
        }
        entries.forEach(::drop)
    }

    private fun drop(entry: Entry) {
        entry.live = false
        entry.job.cancel()
    }

    private fun startOrJoin(url: String): Deferred<AudioEnvelope.Envelopes?>? {
        if (cache.get(url) != null) return null
        val entry: Entry
        synchronized(inFlight) {
            inFlight[url]?.let { return it.job }
            evictOldestWaiterLocked()
            entry = Entry(url)
            entry.job = scope.async { run(entry) }
            inFlight[url] = entry
        }
        entry.job.invokeOnCompletion {
            synchronized(inFlight) { if (inFlight[url] === entry) inFlight.remove(url) }
        }
        return entry.job
    }

    /** Make room for one more request by dropping waiters, oldest first. Runners are untouchable. */
    private fun evictOldestWaiterLocked() {
        while (inFlight.size >= maxPending) {
            val victim = inFlight.values.firstOrNull { !it.running } ?: return
            inFlight.remove(victim.url)
            drop(victim)
        }
    }

    private suspend fun run(entry: Entry): AudioEnvelope.Envelopes? = permits.withPermit {
        entry.running = true
        if (!entry.live) return@withPermit null
        val result = try {
            decode(entry.url) { entry.live }
        } catch (e: CancellationException) {
            throw e
        } catch (e: Exception) {
            // Contained here on purpose: this runs in an `async` whose failure would otherwise take
            // down the playback manager's whole scope, killing the position tick with it.
            Log.w(TAG, "envelope decode failed for ${entry.url}: ${e.message}")
            null
        }
        if (result != null && entry.live) cache.put(entry.url, result)
        result
    }

    companion object {
        private const val TAG = "EnvelopePrefetch"

        /**
         * Decoders alive at once. Two, not one: a bar tapped while another clip is warming must not
         * queue behind it. Higher would start competing for the same hardware decoders.
         */
        const val MAX_CONCURRENT = 2

        /** Requests tracked at once, running plus waiting. A screen of bars, and no backlog. */
        const val MAX_PENDING = 4
    }
}
