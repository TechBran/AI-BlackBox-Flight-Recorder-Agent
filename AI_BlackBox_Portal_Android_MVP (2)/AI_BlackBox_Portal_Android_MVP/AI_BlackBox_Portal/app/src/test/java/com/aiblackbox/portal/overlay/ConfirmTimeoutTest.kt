package com.aiblackbox.portal.overlay

import kotlinx.coroutines.CompletableDeferred
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * (I1, M4) The fail-safe TIMEOUT semantics the overlays ([OverlayConfirmUi] +
 * [OverlayCredentialHandoff]) are wired with, via the shared [awaitConfirmOrDeny] primitive.
 *
 * The overlays themselves are framework code (WindowManager + Looper) and are device-verified;
 * what is JVM-unit-testable — and asserted here — is the primitive's contract: a confirm/handoff
 * that NEVER answers must (1) DENY within the configured window T, and (2) tear the overlay down
 * exactly once (no leak). Without this, a PERMISSION prompt raised with nobody at the device would
 * block the NanoHTTPD worker thread that ran the remote dispatch and hang the cloud loop forever.
 *
 * Uses `runTest`'s virtual clock, so the timeout is deterministic and instant (no real 30s wait).
 */
class ConfirmTimeoutTest {

    @Test
    fun `awaitConfirmOrDeny denies and tears down when the answer never arrives`() = runTest {
        var teardowns = 0
        val never = CompletableDeferred<Boolean>() // never completed → awaitAnswer hangs forever

        val result = awaitConfirmOrDeny(
            timeoutMs = DEFAULT_CONFIRM_TIMEOUT_MS,
            onTimeout = { teardowns++ },
            awaitAnswer = { never.await() },
        )

        assertFalse("a timed-out confirm MUST fail-safe to DENY", result)
        assertEquals("the overlay teardown MUST run exactly once on timeout", 1, teardowns)
    }

    @Test
    fun `the deny lands exactly at the configured timeout T (virtual time)`() = runTest {
        val never = CompletableDeferred<Boolean>()
        val start = testScheduler.currentTime

        val result = awaitConfirmOrDeny(
            timeoutMs = 30_000L,
            onTimeout = {},
            awaitAnswer = { never.await() },
        )

        assertFalse(result)
        assertEquals("the DENY must land at the timeout T, not before/after", 30_000L, testScheduler.currentTime - start)
    }

    @Test
    fun `an in-time answer passes through and never tears down`() = runTest {
        var teardowns = 0

        val allowed = awaitConfirmOrDeny(
            timeoutMs = DEFAULT_CONFIRM_TIMEOUT_MS,
            onTimeout = { teardowns++ },
            awaitAnswer = { true }, // user answers immediately
        )
        assertTrue("an in-time ALLOW must pass through unchanged", allowed)

        val denied = awaitConfirmOrDeny(
            timeoutMs = DEFAULT_CONFIRM_TIMEOUT_MS,
            onTimeout = { teardowns++ },
            awaitAnswer = { false }, // user denies immediately
        )
        assertFalse("an in-time explicit DENY must pass through unchanged", denied)

        assertEquals("no teardown on the normal answered path", 0, teardowns)
    }
}
