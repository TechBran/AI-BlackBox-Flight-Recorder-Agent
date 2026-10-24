package com.aiblackbox.portal.data.local

/** A borrowed resource from a [LeaseGate]. [release] is idempotent. */
interface ResourceLease<T> {
    val value: T
    fun release()
}

/**
 * Holds ONE closeable native resource and refuses to close it under a user
 * (A09-data-local-model-03). [LiteRtEngine] keeps its litertlm `Engine` here: every
 * generation [acquire]s a [ResourceLease] for as long as it runs, and [retire] (close,
 * model switch) closes the resource only when no lease is out — otherwise the LAST
 * [ResourceLease.release] closes it. Before, `close()` freed the native engine while a
 * borrowed reference was still inside a multi-second `sendMessageAsync`
 * (use-after-close).
 *
 * Each installed resource keeps its own lease count, so retiring one and installing the
 * next never lets the next one's leases keep the old one alive (or close it early).
 * Generic and free of litertlm types, so it is JVM-unit-testable with a fake resource
 * (a real engine cannot be constructed on the host test JVM).
 */
class LeaseGate<T : Any>(private val closeResource: (T) -> Unit) {

    private class Slot<T : Any>(val value: T) {
        var leases = 0
        var retired = false
    }

    private val lock = Any()
    private var current: Slot<T>? = null

    private inner class Lease(private val slot: Slot<T>) : ResourceLease<T> {
        override val value: T get() = slot.value
        private var released = false

        override fun release() {
            val toClose = synchronized(lock) {
                if (released) return
                released = true
                slot.leases--
                if (slot.retired && slot.leases == 0) slot.value else null
            }
            toClose?.let(::closeQuietly)
        }
    }

    /** The live resource without leasing it (for "is it loaded" checks only). */
    fun peek(): T? = synchronized(lock) { current?.value }

    /** Lease the live resource for one use, or null when none is installed. */
    fun acquire(): ResourceLease<T>? = synchronized(lock) {
        val slot = current ?: return null
        slot.leases++
        Lease(slot)
    }

    /** Make [value] the live resource, retiring any previous one (see [retire]). */
    fun install(value: T) {
        val toClose = synchronized(lock) {
            val prior = current
            current = Slot(value)
            prior?.let(::retireLocked)
        }
        toClose?.let(::closeQuietly)
    }

    /** Retire the live resource: closed now when no lease is out, else by the last release. */
    fun retire() {
        val toClose = synchronized(lock) {
            val prior = current
            current = null
            prior?.let(::retireLocked)
        }
        toClose?.let(::closeQuietly)
    }

    /** Leases out on the live resource (diagnostics and tests). */
    fun leasesOut(): Int = synchronized(lock) { current?.leases ?: 0 }

    // Returns the resource to close NOW (outside the lock), or null when a lease defers it.
    private fun retireLocked(slot: Slot<T>): T? {
        slot.retired = true
        return if (slot.leases == 0) slot.value else null
    }

    private fun closeQuietly(value: T) {
        runCatching { closeResource(value) }
    }
}
