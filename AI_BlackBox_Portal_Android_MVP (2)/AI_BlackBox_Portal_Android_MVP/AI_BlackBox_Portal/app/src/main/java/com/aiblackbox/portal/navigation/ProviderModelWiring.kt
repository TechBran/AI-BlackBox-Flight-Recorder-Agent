package com.aiblackbox.portal.navigation

import com.aiblackbox.portal.data.store.BlackBoxStore
import com.aiblackbox.portal.util.Constants
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.first

/**
 * The provider and model keys the app's wiring reads and writes: [BlackBoxStore]'s own
 * members, behind an interface so the wiring can be tested without a DataStore.
 */
internal interface ProviderModelStore {
    val provider: Flow<String>
    val model: Flow<String>
    suspend fun setProvider(value: String)
    suspend fun setModel(value: String)
    suspend fun setProviderAndModel(provider: String, liveModelIds: suspend (String) -> Collection<String>?)
    suspend fun selectModel(provider: String, model: String)
}

/** This store as the wiring's [ProviderModelStore]; every call goes straight through. */
internal fun BlackBoxStore.providerModels(): ProviderModelStore {
    val store = this
    return object : ProviderModelStore {
        override val provider: Flow<String> get() = store.provider
        override val model: Flow<String> get() = store.model
        override suspend fun setProvider(value: String) = store.setProvider(value)
        override suspend fun setModel(value: String) = store.setModel(value)
        override suspend fun setProviderAndModel(
            provider: String,
            liveModelIds: suspend (String) -> Collection<String>?,
        ) = store.setProviderAndModel(provider, liveModelIds)
        override suspend fun selectModel(provider: String, model: String) = store.selectModel(provider, model)
    }
}

/**
 * The app's provider and model wiring, out of NativeMainActivity's setContent (audit
 * A01-activities-root-10 / NEW-android-1, rank 76): the composer pill's provider switch and
 * the screen it opens, the model picks of the composer and of the CU and robotics screens,
 * and the provider those two screens set when they open. In one place so every surface keeps
 * going through BlackBoxStore's shared paths (W22); the Settings sheet takes the same paths
 * through SettingsViewModel.
 */
internal class ProviderModelWiring(
    private val store: ProviderModelStore,
    /** The live catalog's ids for a provider, or null when it cannot be read. */
    private val liveModelIds: suspend (String) -> Collection<String>?,
) {
    /**
     * The composer pill's provider switch: Auto at once (so e.g. a Claude id never goes to
     * Gemini), then this provider's remembered pick only if its live list still offers it.
     * The same path as the Settings sheet's provider dropdown.
     */
    suspend fun switchProvider(newProvider: String) {
        store.setProviderAndModel(newProvider) { liveModelIds(it) }
    }

    /** A model pick, remembered per provider too (model_<provider>). */
    suspend fun selectModel(provider: String, model: String) {
        store.selectModel(provider, model)
    }

    /**
     * A screen opened: the computer-use and robotics screens make their provider the
     * current one. [route] is the destination's registered pattern, so only its path
     * counts (computer_use carries "?liveDevice={liveDevice}" for the pill hand-off).
     */
    suspend fun onRouteEntered(route: String?) {
        // destination.route is the registered PATTERN — for
        // computer_use it now carries "?liveDevice={liveDevice}"
        // (the pill hand-off), so compare on the path segment.
        val path = route?.substringBefore('?')
        if (path == Routes.COMPUTER_USE) {
            if (store.provider.first() != "computer-use") {
                // The composer's switch path (W22): Auto at once, then
                // the remembered CU pick only if the live CU catalog
                // still lists it, never the previous provider's id.
                store.setProviderAndModel("computer-use") { liveModelIds(it) }
            }
        }
        if (path == Routes.ROBOTICS) {
            val currentProv = store.provider.first()
            if (currentProv != "robotics") {
                store.setProvider("robotics")
                val erModels = Constants.MODEL_CONFIG["robotics"] ?: emptyList()
                val currentMdl = store.model.first()
                if (erModels.isNotEmpty() && erModels.none { it.first == currentMdl }) {
                    store.setModel(erModels.first().first)
                }
            }
        }
    }

    companion object {
        /** The screen a provider lives on, which picking it in the composer opens; null for chat. */
        fun dedicatedRoute(provider: String): String? = when (provider) {
            "computer-use" -> Routes.COMPUTER_USE
            "robotics" -> Routes.ROBOTICS
            "gemini-live", "grok-live", "realtime" -> Routes.VOICE
            "agents" -> Routes.AGENT
            "gemini-agents" -> Routes.GEMINI_AGENT
            else -> null
        }
    }
}
