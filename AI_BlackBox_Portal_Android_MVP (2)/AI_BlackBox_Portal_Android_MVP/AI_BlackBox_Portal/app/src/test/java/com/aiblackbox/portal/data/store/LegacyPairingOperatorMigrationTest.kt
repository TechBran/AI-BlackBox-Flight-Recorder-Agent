package com.aiblackbox.portal.data.store

import androidx.datastore.core.DataMigration
import androidx.datastore.core.DataStore
import androidx.datastore.preferences.core.PreferenceDataStoreFactory
import androidx.datastore.preferences.core.Preferences
import androidx.datastore.preferences.core.edit
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
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder

/**
 * A09-data-local-model-05 — the operator a pairing QR carries was written to the legacy
 * `bbx_prefs` SharedPreferences and never read again (the DataStore fell back to a seed
 * name instead). [LegacyPairingOperatorMigration] moves it into the DataStore ONCE, through
 * a REAL Preferences DataStore on a temp file (the JVM factory BlackBoxStore's delegate
 * wraps), with the SharedPreferences read behind a lambda.
 */
class LegacyPairingOperatorMigrationTest {

    @get:Rule val tmp = TemporaryFolder()

    private val jobs = mutableListOf<Job>()

    @After fun closeStores() = runBlocking { jobs.forEach { it.cancelAndJoin() } }

    private val file by lazy { tmp.newFolder().resolve("bbx_settings.preferences_pb") }

    private fun open(migrations: List<DataMigration<Preferences>> = emptyList()): DataStore<Preferences> {
        val job = SupervisorJob().also { jobs += it }
        return PreferenceDataStoreFactory.create(
            migrations = migrations,
            scope = CoroutineScope(Dispatchers.IO + job),
            produceFile = { file },
        )
    }

    private suspend fun closeAll() { jobs.forEach { it.cancelAndJoin() }; jobs.clear() }

    private fun migration(legacy: String?) = LegacyPairingOperatorMigration { legacy }

    @Test fun `the QR-paired operator is moved into a store that has none`() = runBlocking {
        val prefs = open(listOf(migration("Ana"))).data.first()
        assertEquals("Ana", prefs[BlackBoxStore.KEY_OPERATOR])
        assertEquals(true, prefs[LegacyPairingOperatorMigration.KEY_MIGRATED])
    }

    @Test fun `an operator already picked in the app wins over the old QR one`() = runBlocking {
        open().edit { it[BlackBoxStore.KEY_OPERATOR] = "Ben" }
        closeAll()
        assertEquals("Ben", open(listOf(migration("Ana"))).data.first()[BlackBoxStore.KEY_OPERATOR])
    }

    @Test fun `a blank stored operator counts as none`() = runBlocking {
        open().edit { it[BlackBoxStore.KEY_OPERATOR] = "" }
        closeAll()
        assertEquals("Ana", open(listOf(migration("  Ana "))).data.first()[BlackBoxStore.KEY_OPERATOR])
    }

    @Test fun `no legacy operator leaves the store unresolved`() = runBlocking {
        for (legacy in listOf(null, "", "   ")) {
            val at = tmp.newFolder().resolve("bbx_settings.preferences_pb")
            val job = SupervisorJob().also { jobs += it }
            val prefs = PreferenceDataStoreFactory.create(
                migrations = listOf(migration(legacy)),
                scope = CoroutineScope(Dispatchers.IO + job),
                produceFile = { at },
            ).data.first()
            assertNull("legacy=$legacy", prefs[BlackBoxStore.KEY_OPERATOR])
            assertEquals("", BlackBoxStore.operatorOf(prefs))
            assertEquals(true, prefs[LegacyPairingOperatorMigration.KEY_MIGRATED])
        }
    }

    @Test fun `it runs once - a later legacy value never overwrites the app's own pick`() = runBlocking {
        val first = open(listOf(migration("Ana")))
        assertEquals("Ana", first.data.first()[BlackBoxStore.KEY_OPERATOR])
        assertFalse(migration("Ana").shouldMigrate(first.data.first()))
        // The operator switches in the app; the stale QR value is still in bbx_prefs.
        first.edit { it[BlackBoxStore.KEY_OPERATOR] = "Ben" }
        closeAll()
        assertEquals("Ben", open(listOf(migration("Ana"))).data.first()[BlackBoxStore.KEY_OPERATOR])
    }
}
