package com.aiblackbox.portal.data.store

import com.aiblackbox.portal.data.model.UiMessage
import kotlinx.coroutines.runBlocking
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.File

/**
 * Chat history is written atomically and a damaged file is moved aside, not
 * reset to empty and then overwritten by the next save (audit
 * A09-data-local-model-11). Plain JVM test over a temp dir.
 */
class ChatHistoryStoreTest {

    @get:Rule
    val tmp = TemporaryFolder()

    private fun store(dir: File) = ChatHistoryStore { dir }

    private fun msg(role: String, content: String) =
        UiMessage(role = role, content = content)

    @Test
    fun `round trip keeps completed messages only`() = runBlocking {
        val dir = tmp.newFolder("chat_history")
        val store = store(dir)
        store.save("Ann", listOf(msg("user", "hi"), msg("assistant", "hello"),
            UiMessage(role = "assistant", content = "partial", isStreaming = true)))

        val loaded = store.load("Ann")
        assertEquals(listOf("hi", "hello"), loaded.map { it.content })
        assertEquals(listOf("history_Ann.json"), dir.list()!!.sorted())
    }

    @Test
    fun `a truncated file is quarantined instead of overwritten by the next save`() = runBlocking {
        val dir = tmp.newFolder("chat_history")
        val store = store(dir)
        store.save("Ann", listOf(msg("user", "one"), msg("assistant", "two")))
        val file = File(dir, "history_Ann.json")
        val torn = file.readText().take(25)
        file.writeText(torn)

        assertTrue(store.load("Ann").isEmpty())
        store.save("Ann", listOf(msg("user", "fresh")))

        val quarantined = dir.listFiles()!!.filter { it.name.startsWith("history_Ann.json.corrupt-") }
        assertEquals(1, quarantined.size)
        assertEquals(torn, quarantined[0].readText())
        assertEquals(listOf("fresh"), store.load("Ann").map { it.content })
    }

    @Test
    fun `atomic write replaces the whole file and leaves no temp file`() {
        val dir = tmp.newFolder("store")
        val file = File(dir, "data.json")
        AtomicTextFile.write(file, "first version, longer than the next one")
        AtomicTextFile.write(file, "second")
        assertEquals("second", file.readText())
        assertEquals(listOf("data.json"), dir.list()!!.sorted())
    }

    @Test
    fun `quarantine keeps every damaged copy`() {
        val dir = tmp.newFolder("store")
        val file = File(dir, "data.json")
        file.writeText("bad 1")
        val first = AtomicTextFile.quarantine(file, nowMillis = 1000L)
        file.writeText("bad 2")
        val second = AtomicTextFile.quarantine(file, nowMillis = 1000L)
        assertTrue(first != null && second != null && first != second)
        assertEquals(setOf("bad 1", "bad 2"), setOf(first!!.readText(), second!!.readText()))
        assertTrue(!file.exists())
    }
}
