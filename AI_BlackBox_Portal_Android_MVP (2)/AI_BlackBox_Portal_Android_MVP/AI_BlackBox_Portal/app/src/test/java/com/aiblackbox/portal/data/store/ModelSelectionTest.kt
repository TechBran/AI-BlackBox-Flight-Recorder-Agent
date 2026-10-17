package com.aiblackbox.portal.data.store

import androidx.datastore.core.DataMigration
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
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * W22 / A11-settings-generation-06 — the provider + model selection, through a REAL
 * Preferences DataStore on a temp file (the JVM factory BlackBoxStore's delegate wraps),
 * so the edits, the migration hook and the run-once flag are exercised exactly as the
 * app runs them. BlackBoxStore itself needs an Android Context, so it is a thin
 * delegate onto [ModelSelection] and [RetiredModelIdMigration]; those are what's tested.
 *
 * Coverage:
 *  1. migration: retired ids rewritten in the global `model` key AND every
 *     `model_<provider>` key — to a named successor where the key's provider takes a
 *     Claude id, else to Auto; any other retired id (the server's pattern) → Auto; a
 *     Claude id or the on-device slug left in the global key under a provider that
 *     cannot serve it → Auto; unknown ids and non-model keys untouched; flag stamped.
 *  2. migration runs ONCE: a retired id written after the stamped run survives a reopen.
 *  3. provider switch: the previous provider's (foreign) id never survives — Auto at
 *     once, before the live list is even asked for; a stale/retired/foreign
 *     `model_<provider>` is never restored; a still-live one is; re-selecting the
 *     active provider keeps the model it is already sending.
 *  4. a pick made while the live list is in flight is never clobbered by the restore.
 *  5. model picks: global + `model_<provider>` together for the active provider; the
 *     on-device Model Manager's "Use" writes ONLY `model_local`, never the global key.
 */
class ModelSelectionTest {

    @get:Rule val tmp = TemporaryFolder()

    private val jobs = mutableListOf<Job>()

    @After fun closeStores() = runBlocking { jobs.forEach { it.cancelAndJoin() } }

    private val file by lazy { tmp.newFolder().resolve("bbx_settings.preferences_pb") }

    /** Open a DataStore on [at]; [closeAll] releases it so the same file can be reopened. */
    private fun open(
        migrations: List<DataMigration<Preferences>> = emptyList(),
        at: java.io.File = file,
    ): DataStore<Preferences> {
        val job = SupervisorJob().also { jobs += it }
        return PreferenceDataStoreFactory.create(
            migrations = migrations,
            scope = CoroutineScope(Dispatchers.IO + job),
            produceFile = { at },
        )
    }

    /** A fresh store seeded with [pairs] (pre-update state), reopened the way the app opens it. */
    private suspend fun migrated(vararg pairs: Pair<String, String>): Preferences {
        val at = tmp.newFolder().resolve("bbx_settings.preferences_pb")
        open(at = at).seed(*pairs)
        closeAll()
        return open(listOf(RetiredModelIdMigration), at).snapshot()
    }

    private suspend fun closeAll() { jobs.forEach { it.cancelAndJoin() }; jobs.clear() }

    private val MODEL = BlackBoxStore.KEY_MODEL
    private val PROVIDER = BlackBoxStore.KEY_PROVIDER
    private fun key(name: String) = stringPreferencesKey(name)

    private suspend fun DataStore<Preferences>.seed(vararg pairs: Pair<String, String>) {
        edit { prefs -> pairs.forEach { (k, v) -> prefs[key(k)] = v } }
    }

    private suspend fun DataStore<Preferences>.snapshot(): Preferences = data.first()

    // ── 1. the one-time retired-id migration ─────────────────────────────────

    @Test fun `the mapping is the one 6071f260 applied to the Android pickers`() {
        assertEquals("claude-opus-4-8", ModelSelection.RETIRED_MODEL_IDS["claude-opus-4-1"])
        assertEquals("claude-sonnet-5-5", ModelSelection.RETIRED_MODEL_IDS["claude-sonnet-4"])
        // No current id is ever a retired key, and no replacement is itself retired.
        for ((retired, current) in ModelSelection.RETIRED_MODEL_IDS) {
            assertFalse("$current is retired", current in ModelSelection.RETIRED_MODEL_IDS)
            assertTrue("$retired maps to blank", current.isNotBlank())
        }
    }

    @Test fun `every successor is on the live Anthropic list and every key is retired server-side`() {
        // On the live list, so a migrated model_anthropic can be restored by a switch back
        // (claude-haiku-4-5 is an alias, NOT on /models/anthropic: never restorable).
        assertEquals("claude-haiku-5-5", ModelSelection.RETIRED_MODEL_IDS["claude-3-5-haiku-20241022"])
        for ((retired, current) in ModelSelection.RETIRED_MODEL_IDS) {
            assertTrue("$current is not live", current in LIVE_ANTHROPIC_IDS)
            assertTrue("$retired is not retired", ModelSelection.RETIRED_CLAUDE_ID.matches(retired))
            assertFalse("$current is retired", ModelSelection.RETIRED_CLAUDE_ID.matches(current))
        }
    }

    @Test fun `the retired pattern is the server's - every retired family, no live or picker id`() {
        // Ids the server's RETIRED_ID_RX retires that the live /v1/models list offered once.
        for (id in listOf(
            "claude-opus-4-20250514", "claude-opus-4-0", "claude-sonnet-4-0", "claude-opus-4",
            "claude-sonnet-4", "claude-opus-4-1", "claude-opus-4-1-20250805", "claude-sonnet-4-20250514",
            "claude-3-5-sonnet-20241022", "claude-3-5-sonnet-latest", "claude-3-opus-20240229",
            "claude-3-haiku-20240307", "claude-3-7-sonnet-latest", "claude-2.1", "claude-2",
            "claude-instant-1.2",
        )) assertTrue(id, ModelSelection.RETIRED_CLAUDE_ID.matches(id))

        // Never a current id: the live list, and every Claude id the static pickers offer.
        val pickerIds = com.aiblackbox.portal.util.Constants.MODEL_CONFIG.values.flatten()
            .map { it.first }.filter { it.startsWith("claude-") }
        assertTrue(pickerIds.isNotEmpty())
        for (id in LIVE_ANTHROPIC_IDS + pickerIds) {
            assertFalse(id, ModelSelection.RETIRED_CLAUDE_ID.matches(id))
        }
    }

    @Test fun `retired ids are rewritten in the global key and every per-provider key`() = runBlocking {
        open().seed(
            "provider" to "anthropic",
            "model" to "claude-opus-4-1",
            "model_anthropic" to "claude-sonnet-4",
            "model_custom" to "claude-opus-4-1-20250805",
            "model_computer-use" to "claude-sonnet-4-20250514",
        )
        closeAll()

        val prefs = open(listOf(RetiredModelIdMigration)).snapshot()

        assertEquals("claude-opus-4-8", prefs[MODEL])
        assertEquals("claude-sonnet-5-5", prefs[key("model_anthropic")])
        // custom ids are alias-qualified: a bare Claude id was never a custom pick → Auto.
        assertEquals("", prefs[key("model_custom")])
        assertEquals("claude-sonnet-5-5", prefs[key("model_computer-use")])
        assertEquals(
            ModelSelection.RETIRED_MODEL_IDS_VERSION,
            prefs[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION],
        )
    }

    @Test fun `unknown ids and non-model keys are left alone`() = runBlocking {
        open().seed(
            "provider" to "gemini",
            "model" to "gemini-2.5-pro",
            "model_anthropic" to "claude-opus-4-7",
            "model_openai" to "gpt-5",
            "model_local" to "gemma-4-e4b-it",
            "model_xai" to "",
            // A retired id under a key that is NOT a model key stays as-is...
            "claude_model" to "claude-opus-4-1",
            "operator" to "claude-sonnet-4",
            // ...and so does one under a model_ key that names no provider.
            "model_unknown" to "claude-opus-4-1",
        )
        closeAll()

        val prefs = open(listOf(RetiredModelIdMigration)).snapshot()

        assertEquals("gemini-2.5-pro", prefs[MODEL])
        assertEquals("claude-opus-4-7", prefs[key("model_anthropic")])
        assertEquals("gpt-5", prefs[key("model_openai")])
        assertEquals("gemma-4-e4b-it", prefs[key("model_local")])
        assertEquals("", prefs[key("model_xai")])
        assertEquals("claude-opus-4-1", prefs[key("claude_model")])
        assertEquals("claude-sonnet-4", prefs[key("operator")])
        assertEquals("claude-opus-4-1", prefs[key("model_unknown")])
        assertEquals("gemini", prefs[PROVIDER])
    }

    @Test fun `a retired id with no named successor becomes Auto`() = runBlocking {
        val prefs = migrated(
            "provider" to "anthropic",
            "model" to "claude-3-5-sonnet-20241022",
            "model_anthropic" to "claude-opus-4-20250514",
            "model_computer-use" to "claude-3-opus-20240229",
        )
        assertEquals("", prefs[MODEL])
        assertEquals("", prefs[key("model_anthropic")])
        assertEquals("", prefs[key("model_computer-use")])
    }

    @Test fun `a Claude id the active provider cannot serve becomes Auto, not a successor`() = runBlocking {
        // A11 residue: the baseline Settings switch left the previous provider's id in the
        // global key — retired or live, a Claude id 404s against any non-Claude provider.
        for ((provider, id) in listOf(
            "gemini" to "claude-opus-4-1", "gemini" to "claude-opus-4-7", "openai" to "claude-sonnet-4",
            "custom" to "claude-opus-4-8", "local" to "claude-sonnet-5-5",
        )) {
            assertEquals("$provider/$id", "", migrated("provider" to provider, "model" to id)[MODEL])
        }
        // No provider key = the gemini default.
        assertEquals("", migrated("model" to "claude-opus-4-1")[MODEL])
        // A Claude provider keeps a live id and gets the successor for a retired one.
        assertEquals("claude-opus-4-7", migrated("provider" to "anthropic", "model" to "claude-opus-4-7")[MODEL])
        assertEquals(
            "claude-sonnet-5-5",
            migrated("provider" to "computer-use", "model" to "claude-sonnet-4-20250514")[MODEL],
        )
    }

    @Test fun `the on-device slug left in the global key becomes Auto`() = runBlocking {
        // A11 residue: the baseline Model Manager "Use" wrote the Gemma slug to BOTH keys.
        val prefs = migrated(
            "provider" to "anthropic",
            "model" to "gemma-4-e4b-it",
            "model_local" to "gemma-4-e4b-it",
        )
        assertEquals("", prefs[MODEL])
        assertEquals("gemma-4-e4b-it", prefs[key("model_local")])
        // A cloud pick next to an on-device one is not touched.
        assertEquals(
            "gemini-2.5-pro",
            migrated("model" to "gemini-2.5-pro", "model_local" to "gemma-4-e4b-it")[MODEL],
        )
    }

    @Test fun `a fresh install gets the flag and nothing else`() = runBlocking {
        val prefs = open(listOf(RetiredModelIdMigration)).snapshot()
        assertNull(prefs[MODEL])
        assertEquals(
            ModelSelection.RETIRED_MODEL_IDS_VERSION,
            prefs[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION],
        )
        assertEquals(1, prefs.asMap().size)
    }

    // ── 2. runs once ─────────────────────────────────────────────────────────

    @Test fun `the migration runs once - a retired id written after it survives a reopen`() = runBlocking {
        open().seed("provider" to "anthropic", "model" to "claude-sonnet-4")
        closeAll()
        val migrated = open(listOf(RetiredModelIdMigration))
        assertEquals("claude-sonnet-5-5", migrated.snapshot()[MODEL])
        assertFalse(RetiredModelIdMigration.shouldMigrate(migrated.snapshot()))

        // Something writes a retired id AFTER the stamped run (the next process start
        // must not rewrite it again: the flag, not the data, decides).
        migrated.seed("model" to "claude-opus-4-1")
        closeAll()

        assertEquals("claude-opus-4-1", open(listOf(RetiredModelIdMigration)).snapshot()[MODEL])
    }

    @Test fun `an older flag version runs the migration again`() = runBlocking {
        open().edit {
            it[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION] = ModelSelection.RETIRED_MODEL_IDS_VERSION - 1
            it[PROVIDER] = "anthropic"
            it[MODEL] = "claude-opus-4-1"
        }
        closeAll()
        val prefs = open(listOf(RetiredModelIdMigration)).snapshot()
        assertEquals("claude-opus-4-8", prefs[MODEL])
        assertEquals(
            ModelSelection.RETIRED_MODEL_IDS_VERSION,
            prefs[ModelSelection.KEY_RETIRED_MODEL_IDS_VERSION],
        )
    }

    // ── 3. provider switch ───────────────────────────────────────────────────

    private val GEMINI_LIVE = listOf("", "gemini-3.1-pro-preview", "gemini-2.5-pro", "gemini-2.5-flash")
    private val ANTHROPIC_LIVE = listOf("", "claude-sonnet-5-5", "claude-opus-4-8", "claude-opus-4-7")

    @Test fun `switching provider never carries the previous provider's id`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "anthropic", "model" to "claude-opus-4-8", "model_anthropic" to "claude-opus-4-8")

        ModelSelection.setProviderAndModel(ds, "gemini") { GEMINI_LIVE }

        val prefs = ds.snapshot()
        assertEquals("gemini", prefs[PROVIDER])
        assertEquals("", prefs[MODEL])
        // The provider we left keeps its remembered pick for the way back.
        assertEquals("claude-opus-4-8", prefs[key("model_anthropic")])
    }

    @Test fun `the switch lands on Auto before the live list is even asked for`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "anthropic", "model" to "claude-opus-4-8", "model_gemini" to "gemini-2.5-pro")
        var seenDuringFetch: Preferences? = null

        ModelSelection.setProviderAndModel(ds, "gemini") {
            seenDuringFetch = ds.snapshot()
            GEMINI_LIVE
        }

        assertEquals("gemini", seenDuringFetch!![PROVIDER])
        assertEquals("", seenDuringFetch!![MODEL])
        assertEquals("gemini-2.5-pro", ds.snapshot()[MODEL])
    }

    @Test fun `a still-live remembered model is restored`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "gemini-2.5-pro", "model_anthropic" to "claude-opus-4-8")
        var askedFor: String? = null

        ModelSelection.setProviderAndModel(ds, "anthropic") { askedFor = it; ANTHROPIC_LIVE }

        assertEquals("anthropic", askedFor)
        assertEquals("anthropic", ds.snapshot()[PROVIDER])
        assertEquals("claude-opus-4-8", ds.snapshot()[MODEL])
    }

    @Test fun `a remembered model the live list no longer offers is never restored`() = runBlocking {
        val ds = open()
        // Retired (gone from the live list) and foreign (another provider's id).
        for (stale in listOf("claude-opus-4-1", "gemini-2.5-pro")) {
            ds.seed("provider" to "gemini", "model" to "gemini-2.5-pro", "model_anthropic" to stale)
            ModelSelection.setProviderAndModel(ds, "anthropic") { ANTHROPIC_LIVE }
            assertEquals("restored $stale", "", ds.snapshot()[MODEL])
        }
    }

    @Test fun `no live list means Auto`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "gemini-2.5-pro", "model_anthropic" to "claude-opus-4-8")

        ModelSelection.setProviderAndModel(ds, "anthropic") { null }

        assertEquals("anthropic", ds.snapshot()[PROVIDER])
        assertEquals("", ds.snapshot()[MODEL])
    }

    @Test fun `nothing remembered means Auto without asking for the live list`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "anthropic", "model" to "claude-opus-4-8")
        var asked = false

        ModelSelection.setProviderAndModel(ds, "openai") { asked = true; listOf("", "gpt-5") }

        assertFalse(asked)
        assertEquals("", ds.snapshot()[MODEL])
    }

    @Test fun `switching to on-device never moves the local slug into the global key`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "gemini-2.5-pro", "model_local" to "gemma-4-e4b-it")

        ModelSelection.setProviderAndModel(ds, "local") { listOf("gemma-4-e4b-it") }

        assertEquals("local", ds.snapshot()[PROVIDER])
        assertEquals("", ds.snapshot()[MODEL])
        assertEquals("gemma-4-e4b-it", ds.snapshot()[key("model_local")])
    }

    @Test fun `re-selecting the active provider keeps the model it is sending`() = runBlocking {
        val ds = open()
        var asked = false
        // No live list (robotics has no model endpoint): the pick must not drop to Auto.
        ds.seed("provider" to "robotics", "model" to "gemini-robotics-er-1.5-preview",
            "model_robotics" to "gemini-robotics-er-1.5-preview")
        ModelSelection.setProviderAndModel(ds, "robotics") { asked = true; null }
        assertEquals("gemini-robotics-er-1.5-preview", ds.snapshot()[MODEL])

        // Offline (live list unknown) — same.
        ds.seed("provider" to "anthropic", "model" to "claude-opus-4-7", "model_anthropic" to "claude-opus-4-7")
        ModelSelection.setProviderAndModel(ds, "anthropic") { asked = true; null }
        assertEquals("claude-opus-4-7", ds.snapshot()[MODEL])
        assertFalse(asked)
    }

    @Test fun `re-selecting the active provider still drops an id it never picked`() = runBlocking {
        val ds = open()
        // A11 residue: a Gemini id being sent with Anthropic; the remembered pick is live.
        ds.seed("provider" to "anthropic", "model" to "gemini-2.5-pro", "model_anthropic" to "claude-opus-4-8")
        ModelSelection.setProviderAndModel(ds, "anthropic") { ANTHROPIC_LIVE }
        assertEquals("claude-opus-4-8", ds.snapshot()[MODEL])

        ds.seed("model" to "gemini-2.5-pro")
        ModelSelection.setProviderAndModel(ds, "anthropic") { null }
        assertEquals("", ds.snapshot()[MODEL])
    }

    // ── 4. a pick made while the live list is in flight wins ─────────────────

    @Test fun `a pick made during the live fetch is not clobbered by the restore`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "", "model_anthropic" to "claude-opus-4-8")

        ModelSelection.setProviderAndModel(ds, "anthropic") {
            // The operator picks before the list comes back: Auto, then a real model.
            ModelSelection.selectModel(ds, "anthropic", "")
            ANTHROPIC_LIVE
        }
        assertEquals("", ds.snapshot()[MODEL])

        ds.seed("model_anthropic" to "claude-opus-4-8")
        ModelSelection.setProviderAndModel(ds, "anthropic") {
            ModelSelection.selectModel(ds, "anthropic", "claude-sonnet-5-5")
            ANTHROPIC_LIVE
        }
        assertEquals("claude-sonnet-5-5", ds.snapshot()[MODEL])
    }

    @Test fun `a second switch during the live fetch owns the model`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "", "model_anthropic" to "claude-opus-4-8")

        ModelSelection.setProviderAndModel(ds, "anthropic") {
            ModelSelection.setProviderAndModel(ds, "openai") { null }
            ANTHROPIC_LIVE
        }

        assertEquals("openai", ds.snapshot()[PROVIDER])
        assertEquals("", ds.snapshot()[MODEL])
    }

    // ── 5. model picks ───────────────────────────────────────────────────────

    @Test fun `a pick for the active provider writes the global and per-provider keys together`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "anthropic", "model" to "")

        ModelSelection.selectModel(ds, "anthropic", "claude-opus-4-8")

        assertEquals("claude-opus-4-8", ds.snapshot()[MODEL])
        assertEquals("claude-opus-4-8", ds.snapshot()[key("model_anthropic")])
    }

    @Test fun `a pick for a provider that is not active never touches the global key`() = runBlocking {
        val ds = open()
        ds.seed("provider" to "gemini", "model" to "gemini-2.5-pro")

        ModelSelection.selectModel(ds, "anthropic", "claude-opus-4-8")

        assertEquals("gemini-2.5-pro", ds.snapshot()[MODEL])
        assertEquals("claude-opus-4-8", ds.snapshot()[key("model_anthropic")])
    }

    @Test fun `a CU screen pick is what a switch back to computer-use restores`() = runBlocking {
        val ds = open()
        // An older pick is remembered for computer-use (Settings, composer, or baseline).
        ds.seed("provider" to "computer-use", "model" to "claude-opus-4-6", "model_computer-use" to "claude-opus-4-6")
        val cuLive = listOf("", "claude-opus-4-6", "gpt-5.5", "gemini-2.5-computer-use-preview-10-2025")

        // The CU screen's picker (NavGraph onModelChange → selectModel(provider, it)).
        ModelSelection.selectModel(ds, "computer-use", "gpt-5.5")
        ModelSelection.setProviderAndModel(ds, "gemini") { GEMINI_LIVE }
        ModelSelection.setProviderAndModel(ds, "computer-use") { cuLive }

        assertEquals("gpt-5.5", ds.snapshot()[MODEL])
    }

    @Test fun `the absent provider key counts as the gemini default`() = runBlocking {
        val ds = open()

        ModelSelection.selectModel(ds, "gemini", "gemini-2.5-flash")

        assertEquals("gemini-2.5-flash", ds.snapshot()[MODEL])
    }

    @Test fun `the on-device Use action writes only model_local`() = runBlocking {
        val ds = open()
        // Cloud provider active: its model must survive the Model Manager's "Use".
        ds.seed("provider" to "anthropic", "model" to "claude-opus-4-8")
        ModelSelection.selectModel(ds, "local", "gemma-4-e4b-it")
        assertEquals("claude-opus-4-8", ds.snapshot()[MODEL])
        assertEquals("gemma-4-e4b-it", ds.snapshot()[key("model_local")])

        // Even with on-device active, the slug lives in model_local only.
        ds.seed("provider" to "local", "model" to "")
        ModelSelection.selectModel(ds, "local", "gemma-4-e2b-it")
        assertEquals("", ds.snapshot()[MODEL])
        assertEquals("gemma-4-e2b-it", ds.snapshot()[key("model_local")])
    }

    private companion object {
        /** The server's LIVE_IDS (Orchestrator/tests/test_retired_claude_ids.py): /v1/models, 2026-10-07. */
        val LIVE_ANTHROPIC_IDS = listOf(
            "claude-haiku-5-5", "claude-sonnet-5-5", "claude-opus-5-5", "claude-fable-5-1",
            "claude-opus-5", "claude-sonnet-5", "claude-fable-5", "claude-opus-4-8",
            "claude-opus-4-7", "claude-sonnet-4-6", "claude-opus-4-6",
            "claude-opus-4-5-20251101", "claude-haiku-4-5-20251001", "claude-sonnet-4-5-20250929",
        )
    }
}
