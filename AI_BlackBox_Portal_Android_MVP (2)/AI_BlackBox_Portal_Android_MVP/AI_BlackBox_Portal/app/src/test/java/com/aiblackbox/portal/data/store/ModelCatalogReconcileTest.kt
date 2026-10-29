package com.aiblackbox.portal.data.store

import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
import androidx.datastore.preferences.core.stringPreferencesKey
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancelAndJoin
import kotlinx.coroutines.flow.first
import kotlinx.coroutines.runBlocking
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNull
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * Stored model ids a provider's fresh live catalog no longer lists are replaced (audit
 * A01-activities-root-10, rank 76): the same-family successor when the catalog lists it,
 * else Auto. Through a real Preferences DataStore on a temp file, like ModelSelectionTest.
 */
class ModelCatalogReconcileTest {

    @get:Rule val tmp = TemporaryFolder()

    private val jobs = mutableListOf<Job>()

    @After fun closeStores() = runBlocking { jobs.forEach { it.cancelAndJoin() } }

    private fun open(): DataStore<Preferences> {
        val job = SupervisorJob().also { jobs += it }
        val at = tmp.newFolder().resolve("bbx_settings.preferences_pb")
        return PreferenceDataStoreFactory.create(scope = CoroutineScope(Dispatchers.IO + job), produceFile = { at })
    }

    private fun key(name: String) = stringPreferencesKey(name)

    private suspend fun DataStore<Preferences>.seed(vararg pairs: Pair<String, String>) {
        edit { prefs -> pairs.forEach { (k, v) -> prefs[key(k)] = v } }
    }

    private val liveAnthropic = listOf("claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5", "claude-opus-4-8")

    @Test fun `an id the catalog lists, and Auto, stay`() {
        assertNull(ModelSelection.replacementFor("claude-opus-4-8", liveAnthropic))
        assertNull(ModelSelection.replacementFor("", liveAnthropic))
    }

    @Test fun `a dropped Claude id goes to its family's newest when the catalog lists it`() {
        // Not retired by the server's pattern, just gone from the live list.
        assertEquals("claude-opus-5-5", ModelSelection.replacementFor("claude-opus-4-6", liveAnthropic))
        assertEquals("claude-sonnet-5-5", ModelSelection.replacementFor("claude-sonnet-4-6", liveAnthropic))
        // A named retired id takes its table entry.
        assertEquals("claude-haiku-5-5", ModelSelection.replacementFor("claude-3-5-haiku-20241022", liveAnthropic))
        // The family's newest is not on this catalog (CU lists fewer models): Auto.
        assertEquals("", ModelSelection.replacementFor("claude-sonnet-4-6", listOf("claude-opus-5-5", "gemini-cu")))
    }

    @Test fun `any other missing id goes to Auto`() {
        assertEquals("", ModelSelection.replacementFor("gpt-4o", listOf("gpt-5.5", "gpt-5.5-mini")))
        assertEquals("", ModelSelection.replacementFor("old-box::gemma", listOf("box::gemma-26b")))
        assertEquals("", ModelSelection.replacementFor("claude-2.1", liveAnthropic))
    }

    @Test fun `the per-provider pick and the active provider's sent id are replaced in one edit`() = runBlocking {
        val store = open()
        store.seed(
            "provider" to "anthropic",
            "model" to "claude-opus-4-6",
            "model_anthropic" to "claude-sonnet-4-6",
            "model_openai" to "gpt-4o",
        )

        val changed = ModelSelection.reconcileWithCatalog(store, "anthropic", liveAnthropic)

        val prefs = store.data.first()
        assertEquals(2, changed)
        assertEquals("claude-opus-5-5", prefs[BlackBoxStore.KEY_MODEL])
        assertEquals("claude-sonnet-5-5", prefs[key("model_anthropic")])
        assertEquals("another provider's pick is its own catalog's business", "gpt-4o", prefs[key("model_openai")])
    }

    @Test fun `the sent id belongs to the active provider only`() = runBlocking {
        val store = open()
        store.seed("provider" to "gemini", "model" to "gemini-2.5-pro", "model_anthropic" to "claude-opus-4-6")

        ModelSelection.reconcileWithCatalog(store, "anthropic", liveAnthropic)

        val prefs = store.data.first()
        assertEquals("gemini-2.5-pro", prefs[BlackBoxStore.KEY_MODEL])
        assertEquals("claude-opus-5-5", prefs[key("model_anthropic")])
    }

    @Test fun `a foreign id left in the sent key is replaced, listed picks are untouched`() = runBlocking {
        val store = open()
        // An anthropic id left in the global key under openai (A11 residue) can only 404.
        store.seed("provider" to "openai", "model" to "claude-opus-4-8", "model_openai" to "gpt-5.5")

        val changed = ModelSelection.reconcileWithCatalog(store, "openai", listOf("gpt-5.5"))

        val prefs = store.data.first()
        assertEquals(1, changed)
        assertEquals("", prefs[BlackBoxStore.KEY_MODEL])
        assertEquals("gpt-5.5", prefs[key("model_openai")])
    }

    @Test fun `an empty catalog and the on-device provider change nothing`() = runBlocking {
        val store = open()
        store.seed("provider" to "local", "model" to "", "model_local" to "gemma-4-e4b", "model_anthropic" to "claude-opus-4-6")

        assertEquals(0, ModelSelection.reconcileWithCatalog(store, "anthropic", emptyList()))
        assertEquals(0, ModelSelection.reconcileWithCatalog(store, "local", listOf("something")))

        val prefs = store.data.first()
        assertEquals("gemma-4-e4b", prefs[key("model_local")])
        assertEquals("claude-opus-4-6", prefs[key("model_anthropic")])
    }
}
