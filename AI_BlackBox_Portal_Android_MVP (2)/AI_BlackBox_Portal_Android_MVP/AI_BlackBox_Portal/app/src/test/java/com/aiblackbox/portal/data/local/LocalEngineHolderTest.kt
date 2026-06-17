package com.aiblackbox.portal.data.local

import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * Task R2-C — process-level warm-engine holder + the "use-holder-iff-it-matches"
 * decision.
 *
 * Strategy mirrors [LiteRtMappersTest] / [ChatViewModelLocalWarmTest]: the litertlm
 * 0.13.1 artifact is Java-21 bytecode, so constructing a real [LiteRtEngine] on the
 * host JDK-17 test JVM throws UnsupportedClassVersionError (see the Mappers header in
 * LiteRtEngine.kt). So the engine-touching surface of [LocalEngineHolder]
 * (set/clearAndClose with a live engine) is framework/device-verified, and this
 * exercises the PURE, primitive-typed parts directly:
 *  - [engineSourceFor] — the prefer-holder-else-build-own decision the ViewModel
 *    applies in [com.aiblackbox.portal.ui.chat.ChatViewModel.localProviderOrWire].
 *  - the EMPTY-holder [LocalEngineHolder.getOrNull] / identity defaults (no engine
 *    constructed), proving the graceful-fallback starting state.
 *
 * Proving the decision proves the integration: the ViewModel borrows the warm engine
 * ONLY when engineSourceFor == USE_HOLDER and otherwise builds (and owns) its own —
 * the pre-R2-C path, also taken whenever the service never started.
 */
class LocalEngineHolderTest {

    private val activePath = "/data/user/0/com.aiblackbox.portal/files/local_models/gemma-4-e2b.litertlm"
    private val otherPath = "/data/user/0/com.aiblackbox.portal/files/local_models/gemma-4-e4b.litertlm"

    // -- 1. Holder matches the active model -> USE_HOLDER --

    @Test fun `holder present and matching the active model uses the holder`() {
        assertEquals(
            EngineSource.USE_HOLDER,
            engineSourceFor(
                holderHasEngine = true,
                holderModelPath = activePath,
                activeModelPath = activePath,
            ),
        )
    }

    // -- 2. Holder empty -> BUILD_OWN (the graceful fallback / service-not-running) --

    @Test fun `empty holder builds its own engine`() {
        assertEquals(
            "no warm engine to borrow -> the ViewModel builds + owns its own (fallback)",
            EngineSource.BUILD_OWN,
            engineSourceFor(
                holderHasEngine = false,
                holderModelPath = null,
                activeModelPath = activePath,
            ),
        )
    }

    @Test fun `empty holder builds its own even if a stale path lingers`() {
        // Defensive: a path with no engine still resolves to BUILD_OWN (engine-presence
        // is checked first), so a torn-down holder never yields a phantom USE_HOLDER.
        assertEquals(
            EngineSource.BUILD_OWN,
            engineSourceFor(
                holderHasEngine = false,
                holderModelPath = activePath,
                activeModelPath = activePath,
            ),
        )
    }

    // -- 3. Holder present but for a DIFFERENT model -> BUILD_OWN (user switched) --

    @Test fun `holder for a different model builds its own engine`() {
        assertEquals(
            "the held engine is the wrong bundle -> build the active one",
            EngineSource.BUILD_OWN,
            engineSourceFor(
                holderHasEngine = true,
                holderModelPath = otherPath,
                activeModelPath = activePath,
            ),
        )
    }

    @Test fun `holder present with null path never matches`() {
        assertEquals(
            EngineSource.BUILD_OWN,
            engineSourceFor(
                holderHasEngine = true,
                holderModelPath = null,
                activeModelPath = activePath,
            ),
        )
    }

    // -- 4. A blank active path can never match (defensive) -> BUILD_OWN --

    @Test fun `a blank active model path falls back to build own`() {
        assertEquals(
            EngineSource.BUILD_OWN,
            engineSourceFor(
                holderHasEngine = true,
                holderModelPath = "",
                activeModelPath = "",
            ),
        )
    }

    // -- 5. Empty-holder starting state (no engine constructed) --

    @Test fun `a freshly cleared holder is empty`() {
        // clearAndClose with nothing held is a safe no-op (does not touch a native
        // engine) and leaves the holder empty -> getOrNull null, identity cleared.
        LocalEngineHolder.clearAndClose()
        assertNull("no engine held after clear", LocalEngineHolder.getOrNull())
        assertNull("no model path after clear", LocalEngineHolder.modelPath)
        assertNull("no delegate after clear", LocalEngineHolder.delegate)
    }

    // -- 6. shouldWarm — idempotent FG-service warm (R2-C follow-up) --
    //
    // The service's warm fires on every provider toggle / model switch. A redundant
    // re-warm for the ALREADY-PINNED model must NOT build a new engine, because the
    // subsequent set() would close the live engine the consumer borrowed
    // (localEngineFromHolder=true) — re-triggering the ~10-75s cold reload R2-C
    // prevents and leaking the superseded engine. shouldWarm gates that.

    @Test fun `already pinned same model does not warm again`() {
        // Holder already holds an engine for the active model -> skip build/load/set,
        // so the borrowed engine is never closed by a redundant re-warm.
        assertFalse(
            "same model already pinned + warm -> no rebuild (don't close the borrowed engine)",
            shouldWarm(
                holderHasEngine = true,
                holderModelPath = activePath,
                targetModelPath = activePath,
            ),
        )
    }

    @Test fun `empty holder warms`() {
        // Nothing pinned yet (service first start / OS reclaimed the process) -> warm.
        assertTrue(
            "nothing pinned -> warm-load the active model",
            shouldWarm(
                holderHasEngine = false,
                holderModelPath = null,
                targetModelPath = activePath,
            ),
        )
    }

    @Test fun `empty holder warms even if a stale path lingers`() {
        // Engine-presence is checked first, so a torn-down holder with a lingering path
        // still warms (never a phantom "already pinned").
        assertTrue(
            shouldWarm(
                holderHasEngine = false,
                holderModelPath = activePath,
                targetModelPath = activePath,
            ),
        )
    }

    @Test fun `different model warms (a real model switch rebuilds)`() {
        // The held engine is the wrong bundle (user switched models) -> build + set the
        // new one; closing the superseded engine in set() IS correct here.
        assertTrue(
            "held engine is the wrong bundle -> rebuild the active one (close superseded)",
            shouldWarm(
                holderHasEngine = true,
                holderModelPath = otherPath,
                targetModelPath = activePath,
            ),
        )
    }

    @Test fun `held with null path warms`() {
        // A null held path can never equal a concrete target -> warm (defensive).
        assertTrue(
            shouldWarm(
                holderHasEngine = true,
                holderModelPath = null,
                targetModelPath = activePath,
            ),
        )
    }

    @Test fun `held with blank path warms`() {
        // A blank held path can never equal a concrete target -> warm (defensive).
        assertTrue(
            shouldWarm(
                holderHasEngine = true,
                holderModelPath = "",
                targetModelPath = activePath,
            ),
        )
    }
}
