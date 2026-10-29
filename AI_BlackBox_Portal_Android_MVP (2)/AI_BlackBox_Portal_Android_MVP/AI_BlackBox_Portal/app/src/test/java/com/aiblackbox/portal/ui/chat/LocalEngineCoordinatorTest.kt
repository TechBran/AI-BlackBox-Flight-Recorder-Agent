package com.aiblackbox.portal.ui.chat

import android.app.Application
import com.aiblackbox.portal.data.local.FakeLocalLlm
import com.aiblackbox.portal.data.local.LocalWarmPrefs
import com.aiblackbox.portal.data.local.WarmInflightStore
import kotlinx.coroutines.test.StandardTestDispatcher
import kotlinx.coroutines.test.TestScope
import kotlinx.coroutines.test.advanceUntilIdle
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config

/**
 * The chat's on-device engine side, out of ChatViewModel (rank 76). The wiring against a
 * real bundle needs a device; these pin the gates around it that used to be reachable
 * only through the AndroidViewModel: the provider and auto-warm gates of the preload,
 * the crash-loop skip, a test fake reported READY, and the re-warm a turn defers.
 */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class LocalEngineCoordinatorTest {

    private lateinit var app: Application
    private var local = true
    private var inFlight = false

    @Before fun setUp() {
        app = RuntimeEnvironment.getApplication()
        LocalWarmPrefs.fromContext(app).setAutoWarmEnabled(true)
        WarmInflightStore.fromContext(app).setInflight(false)
        local = true
        inFlight = false
    }

    /**
     * Run the test scheduler until [done]: the preload resolves the engine on the real IO
     * dispatcher, so its continuation lands on the test scheduler a moment later.
     */
    private fun TestScope.settle(done: () -> Boolean = { false }) {
        repeat(40) {
            advanceUntilIdle()
            if (done()) return
            Thread.sleep(5)
        }
        advanceUntilIdle()
    }

    private fun TestScope.coordinator() = LocalEngineCoordinator(
        appContext = app,
        scope = this,
        api = { null },
        isLocalProvider = { local },
        turnInFlight = { inFlight },
    )

    @Test fun `no warm for a cloud provider`() = runTest(StandardTestDispatcher()) {
        local = false
        val c = coordinator()
        c.llmProvider = { FakeLocalLlm() }
        c.preload()
        settle()
        assertEquals(LocalEngineState.IDLE, c.state.value)
    }

    @Test fun `a wired fake with nothing to load reads READY once loaded, IDLE before`() = runTest(StandardTestDispatcher()) {
        val fake = FakeLocalLlm()
        val c = coordinator()
        c.llmProvider = { fake }
        c.preload()
        settle()
        assertEquals(LocalEngineState.IDLE, c.state.value)

        fake.load(java.io.File("model.litertlm"), "cpu")
        c.preload()
        settle { c.state.value == LocalEngineState.READY }
        assertEquals(LocalEngineState.READY, c.state.value)
    }

    @Test fun `no model installed and no box leaves the engine IDLE`() = runTest(StandardTestDispatcher()) {
        val c = coordinator()
        c.preload()
        settle()
        assertEquals(LocalEngineState.IDLE, c.state.value)
    }

    @Test fun `auto-warm off and a crash-looped warm both skip the preload`() = runTest(StandardTestDispatcher()) {
        val fake = FakeLocalLlm().also { it.load(java.io.File("m"), "cpu") }
        LocalWarmPrefs.fromContext(app).setAutoWarmEnabled(false)
        val off = coordinator().apply { llmProvider = { fake } }
        off.preload()
        settle()
        assertEquals("the model loads lazily on the first send", LocalEngineState.IDLE, off.state.value)

        LocalWarmPrefs.fromContext(app).setAutoWarmEnabled(true)
        val store = WarmInflightStore.fromContext(app)
        store.setInflight(true) // the last warm never finished: an OOM kill
        val looped = coordinator().apply { llmProvider = { fake } }
        looped.preload()
        settle()
        assertEquals(LocalEngineState.IDLE, looped.state.value)
        assertFalse("the flag is re-armed so a deliberate send can retry", store.isInflight())
    }

    @Test fun `a deferred re-warm runs once, and not for a cloud provider`() = runTest(StandardTestDispatcher()) {
        val c = coordinator()
        c.llmProvider = { FakeLocalLlm() }
        c.pendingReWarm = true
        local = false
        c.processPendingReWarm()
        assertFalse("consumed", c.pendingReWarm)
        // A cloud provider gets no re-warm: the wiring a test or host set stays.
        assertTrue(c.llmProvider != null)

        local = true
        c.pendingReWarm = true
        c.processPendingReWarm()
        settle { c.llmProvider == null }
        assertFalse(c.pendingReWarm)
        // The re-warm dropped the old wiring (invalidate) before warming again.
        assertEquals(null, c.llmProvider)
    }

    @Test fun `settings are not applied without an active on-device model`() = runTest(StandardTestDispatcher()) {
        val c = coordinator()
        c.applySettings(maxTokens = 4096, topK = null, topP = null, temperature = null)
        settle()
        assertEquals(LocalEngineState.IDLE, c.state.value)
        assertFalse(c.pendingReWarm)
    }
}
