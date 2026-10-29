package com.aiblackbox.portal.navigation

import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test

/**
 * The app's provider and model wiring, out of NativeMainActivity (rank 76): which store path
 * each surface takes, the screen each provider opens, and what opening the computer-use and
 * robotics screens does to the current provider and model. Against a recording store; the
 * store paths themselves are ModelSelectionTest's.
 */
class ProviderModelWiringTest {

    private class RecordingStore(provider: String, model: String) : ProviderModelStore {
        val providerState = MutableStateFlow(provider)
        val modelState = MutableStateFlow(model)
        val calls = mutableListOf<String>()
        var liveIdsSeen: Collection<String>? = null
        override val provider: Flow<String> get() = providerState
        override val model: Flow<String> get() = modelState
        override suspend fun setProvider(value: String) { calls += "setProvider($value)"; providerState.value = value }
        override suspend fun setModel(value: String) { calls += "setModel($value)"; modelState.value = value }
        override suspend fun setProviderAndModel(provider: String, liveModelIds: suspend (String) -> Collection<String>?) {
            calls += "setProviderAndModel($provider)"
            providerState.value = provider
            liveIdsSeen = liveModelIds(provider)
        }
        override suspend fun selectModel(provider: String, model: String) {
            calls += "selectModel($provider, $model)"
        }
    }

    private val asked = mutableListOf<String>()
    private fun wiring(store: RecordingStore) = ProviderModelWiring(store) { provider ->
        asked += provider
        listOf("$provider-live-1", "$provider-live-2")
    }

    @Test fun `the composer's provider switch takes the shared path with the live ids`() = runBlocking {
        val store = RecordingStore("gemini", "gemini-3-pro")
        wiring(store).switchProvider("anthropic")
        assertEquals(listOf("setProviderAndModel(anthropic)"), store.calls)
        assertEquals(listOf("anthropic"), asked)
        assertEquals(listOf("anthropic-live-1", "anthropic-live-2"), store.liveIdsSeen)
    }

    @Test fun `a model pick is remembered per provider`() = runBlocking {
        val store = RecordingStore("anthropic", "")
        wiring(store).selectModel("anthropic", "claude-opus-5-5")
        assertEquals(listOf("selectModel(anthropic, claude-opus-5-5)"), store.calls)
    }

    @Test fun `each special provider opens its own screen and the rest stay in chat`() {
        assertEquals(Routes.COMPUTER_USE, ProviderModelWiring.dedicatedRoute("computer-use"))
        assertEquals(Routes.ROBOTICS, ProviderModelWiring.dedicatedRoute("robotics"))
        for (voice in listOf("gemini-live", "grok-live", "realtime")) {
            assertEquals(voice, Routes.VOICE, ProviderModelWiring.dedicatedRoute(voice))
        }
        assertEquals(Routes.AGENT, ProviderModelWiring.dedicatedRoute("agents"))
        assertEquals(Routes.GEMINI_AGENT, ProviderModelWiring.dedicatedRoute("gemini-agents"))
        for (chat in listOf("gemini", "anthropic", "openai", "xai", "custom", "local", "")) {
            assertNull(chat, ProviderModelWiring.dedicatedRoute(chat))
        }
    }

    @Test fun `a send with an agent provider waits on its agent screen`() {
        assertEquals(Routes.AGENT, ProviderModelWiring.agentRoute("agents"))
        assertEquals(Routes.GEMINI_AGENT, ProviderModelWiring.agentRoute("gemini-agents"))
        for (other in listOf("gemini", "anthropic", "computer-use", "robotics", "gemini-live", "")) {
            assertNull(other, ProviderModelWiring.agentRoute(other))
        }
        // The same screens the composer's provider switch opens for them.
        for (agent in listOf("agents", "gemini-agents")) {
            assertEquals(ProviderModelWiring.dedicatedRoute(agent), ProviderModelWiring.agentRoute(agent))
        }
    }

    @Test fun `opening computer use switches to it through the shared path, once`() = runBlocking {
        val store = RecordingStore("anthropic", "claude-opus-5-5")
        val w = wiring(store)
        // The registered pattern carries the live-device argument.
        w.onRouteEntered(Routes.COMPUTER_USE + "?liveDevice={liveDevice}")
        assertEquals(listOf("setProviderAndModel(computer-use)"), store.calls)
        assertEquals(listOf("computer-use"), asked)

        w.onRouteEntered(Routes.COMPUTER_USE)
        assertEquals("already computer use: nothing", 1, store.calls.size)
    }

    @Test fun `opening robotics sets its provider and its one model when the current is not one`() = runBlocking {
        val erModel = Constants.MODEL_CONFIG.getValue("robotics").first().first
        val store = RecordingStore("gemini", "gemini-3-pro")
        val w = wiring(store)
        w.onRouteEntered(Routes.ROBOTICS)
        assertEquals(listOf("setProvider(robotics)", "setModel($erModel)"), store.calls)
        assertTrue("no catalog is asked for robotics", asked.isEmpty())

        w.onRouteEntered(Routes.ROBOTICS)
        assertEquals("already robotics: nothing", 2, store.calls.size)
    }

    @Test fun `opening robotics keeps a robotics model that is already current`() = runBlocking {
        val erModel = Constants.MODEL_CONFIG.getValue("robotics").first().first
        val store = RecordingStore("gemini", erModel)
        wiring(store).onRouteEntered(Routes.ROBOTICS)
        assertEquals(listOf("setProvider(robotics)"), store.calls)
    }

    @Test fun `other screens leave the provider alone`() = runBlocking {
        val store = RecordingStore("gemini", "gemini-3-pro")
        val w = wiring(store)
        for (route in listOf(Routes.CHAT, Routes.VOICE, Routes.AGENT, "${Routes.CU_LIVE_VIEW}/{sessionId}", null)) {
            w.onRouteEntered(route)
        }
        assertTrue(store.calls.toString(), store.calls.isEmpty())
    }
}
