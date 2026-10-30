package com.aiblackbox.portal.data.wellness

import android.app.Application
import org.junit.Assert.assertFalse
import org.junit.Assert.assertTrue
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.RuntimeEnvironment
import org.robolectric.annotation.Config
import java.io.File

/**
 * Every wellness path resolves under `noBackupFilesDir` (spec §3.2, "Phone-side state"): the
 * app sets allowBackup="true", so the device key, the tokens, the cursor, the id index and the
 * outbox (which holds health values until the box accepts them) must never be anywhere a
 * cloud backup or device transfer copies.
 */
class WellnessNoBackupPathTest {

    private val appData = WellnessFixtures.tempDir("appdata")
    private val noBackup = File(appData, "no_backup").apply { mkdirs() }
    private val filesDir = File(appData, "files").apply { mkdirs() }

    private fun under(dir: File, f: File): Boolean =
        f.canonicalFile.toPath().startsWith(dir.canonicalFile.toPath())

    @Test
    fun `every declared wellness path is under the no-backup dir`() {
        val files = WellnessFiles(noBackup)
        for (f in files.all()) {
            assertTrue("$f escapes no_backup", under(noBackup, f))
            assertFalse("$f is under files/", under(filesDir, f))
        }
    }

    @Test
    fun `everything the collector actually writes lands under the no-backup dir`() = kotlinx.coroutines.runBlocking {
        val files = WellnessFiles(noBackup)
        val binding = WellnessBinding(files)
        binding.installId()
        val intent = binding.beginIntent(WellnessFixtures.OPERATOR)
        binding.acceptBind(WellnessBindRequest(WellnessFixtures.OPERATOR, "en_0badc0de", 1, intent.nonce,
            OriginSigner.encodeKey(ByteArray(32) { 7 })))
        ChangesTokenStore(files.tokens).seed(HcType.SLEEP, "tok")
        DayIndex(files.dayIndex).apply { record("id", HcType.SLEEP, "2026-11-03"); save() }
        WellnessSyncStateStore(files.syncState).update { it.copy(intervalMin = 30) }
        val outbox = WellnessOutbox(files.outbox, files.quarantine)
        outbox.enqueue("{}")
        outbox.drain(send = { WellnessOutbox.SendResult(422) }, newBatchId = { "x" })
        outbox.enqueue("{}")

        val written = appData.walkTopDown().filter { it.isFile }.toList()
        assertTrue(written.isNotEmpty())
        for (f in written) assertTrue("$f was written outside no_backup", under(noBackup, f))
        assertTrue(filesDir.listFiles()!!.isEmpty())
    }

    @Test
    fun `the directory name is fixed`() {
        assertTrue(WellnessFiles(noBackup).root == File(noBackup, "wellness"))
    }
}

/** On a real (Robolectric) context, [WellnessFiles.of] roots at `noBackupFilesDir`, not `filesDir`. */
@RunWith(RobolectricTestRunner::class)
@Config(application = Application::class)
class WellnessNoBackupContextTest {
    @Test
    fun `WellnessFiles of a context is the context's no-backup dir`() {
        val app: Application = RuntimeEnvironment.getApplication()
        val files = WellnessFiles.of(app)
        assertTrue(files.root.canonicalFile.toPath().startsWith(app.noBackupFilesDir.canonicalFile.toPath()))
        assertFalse(files.root.canonicalFile.toPath().startsWith(app.filesDir.canonicalFile.toPath()))
        assertTrue(WellnessGraph.files(app).root == files.root)
    }

    @Test
    fun `a chat history that used health data is written under the no-backup dir only`(): Unit = kotlinx.coroutines.runBlocking {
        val app: Application = RuntimeEnvironment.getApplication()
        val store = com.aiblackbox.portal.data.store.ChatHistoryStore(app)
        val reply = com.aiblackbox.portal.data.model.UiMessage(role = "assistant", content = "You slept 6 h 49 min, resting 57 bpm.",
            wellnessRef = "w-20261028-ab12", wellnessUsed = WellnessUsed("w-20261028-ab12", categories = listOf("sleep")))
        store.save(WellnessFixtures.OPERATOR, listOf(reply))
        val inFiles = app.filesDir.walkTopDown().filter { it.isFile }.toList()
        for (f in inFiles) assertFalse("$f holds a health value", f.readText().contains("57 bpm"))
        val written = app.noBackupFilesDir.walkTopDown().filter { it.isFile && it.readText().contains("57 bpm") }.toList()
        assertTrue("the history is under no_backup", written.isNotEmpty())
        store.clearAll()
    }
}
