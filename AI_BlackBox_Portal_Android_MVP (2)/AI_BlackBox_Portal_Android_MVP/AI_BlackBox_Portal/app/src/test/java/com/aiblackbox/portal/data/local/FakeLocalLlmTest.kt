package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Unit tests for [FakeLocalLlm] — the in-test [LocalLlm] double the rest of
 * Phase 2 (FcLoop, ChatViewModel.sendViaLocalEngine) collects against. The
 * concrete LiteRT-LM engine is Task 2.6 (deferred — needs the AI Edge SDK deps
 * + a device), so this proves the streaming seam itself: a cold Flow that emits
 * incremental text chunks in order, plus the load/close lifecycle the wiring
 * layers depend on.
 *
 * Coverage:
 *   1. generate emits the scripted chunks, in order (the streaming contract).
 *   2. prompt-dependent script — chunks vary by prompt.
 *   3. load records the file + flips isLoaded; generate records prompts.
 *   4. close flips closed.
 *   5. generate-before-load surfaces the configured error (opt-in flag).
 */
class FakeLocalLlmTest {

    @Test
    fun `generate emits the scripted chunks in order`() = runTest {
        val script = listOf("Hello", ", ", "world", "!")
        val llm = FakeLocalLlm(responseChunks = script)
        llm.load(File("/tmp/fake-model.litertlm"))

        val emitted = llm.generate("anything").toList()

        assertEquals("the Flow emits the scripted chunks verbatim, in order", script, emitted)
    }

    @Test
    fun `generate uses the prompt-dependent script when provided`() = runTest {
        val llm = FakeLocalLlm(scriptFor = { prompt -> listOf("echo:", prompt) })
        llm.load(File("/tmp/fake-model.litertlm"))

        assertEquals(listOf("echo:", "ping"), llm.generate("ping").toList())
        assertEquals(listOf("echo:", "pong"), llm.generate("pong").toList())
    }

    @Test
    fun `load records the file and flips isLoaded, and generate records prompts`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("ok"))
        assertFalse("not loaded before load()", llm.isLoaded)
        assertEquals(0, llm.loadCount)
        assertNull(llm.loadedFile)
        assertNull(llm.lastPrompt)
        assertTrue(llm.prompts.isEmpty())

        val modelFile = File("/tmp/fake-model.litertlm")
        llm.load(modelFile, delegate = "gpu")

        assertTrue("isLoaded after load()", llm.isLoaded)
        assertEquals(1, llm.loadCount)
        assertEquals(modelFile, llm.loadedFile)
        assertEquals("gpu", llm.loadedDelegate)

        llm.generate("first").toList()
        llm.generate("second").toList()

        assertEquals(listOf("first", "second"), llm.prompts)
        assertEquals("second", llm.lastPrompt)
    }

    @Test
    fun `close flips closed`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("ok"))
        llm.load(File("/tmp/fake-model.litertlm"))
        assertFalse(llm.closed)

        llm.close()

        assertTrue("closed after close()", llm.closed)
    }

    @Test
    fun `generate before load surfaces the configured error when failIfNotLoaded is set`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("ok"), failIfNotLoaded = true)
        // No load() call.
        val thrown = runCatching { llm.generate("hi").toList() }.exceptionOrNull()
        assertTrue(
            "generate-before-load must surface an IllegalStateException, got $thrown",
            thrown is IllegalStateException,
        )
    }

    @Test
    fun `generate before load is permitted by default (failIfNotLoaded off)`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("ok"))
        // No load() call — default fake is lenient so simple tests don't need load().
        assertEquals(listOf("ok"), llm.generate("hi").toList())
    }
}
