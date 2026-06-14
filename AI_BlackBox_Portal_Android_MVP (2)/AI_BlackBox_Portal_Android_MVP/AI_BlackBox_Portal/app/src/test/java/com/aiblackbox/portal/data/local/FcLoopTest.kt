package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.local.FcLoop.Role
import com.aiblackbox.portal.data.local.FcLoop.Turn
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import kotlinx.coroutines.flow.toList
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.File

/**
 * Unit tests for [FcLoop] — the Phase 2 on-device agent loop. In Phase 2 it has
 * NO tools: it assembles a provider-neutral prompt (persona + history + the new
 * user message) and streams the model's reply deltas straight from
 * [LocalLlm.generate]. Tools / FC-SDK dispatch arrive in Phase 3.
 *
 * Everything is exercised against [FakeLocalLlm] on the JVM — no AI Edge deps,
 * no device. Coverage:
 *   1. runTurn streams the fake's scripted deltas, in order.
 *   2. buildPrompt: persona first, history in order with role markers, ends with
 *      the new user message + an assistant cue.
 *   3. empty history still builds a valid prompt and streams.
 *   4. errors thrown mid-stream propagate to the collector (not swallowed): the
 *      partial deltas arrive, then the exception.
 *   5. a not-loaded engine's error surfaces THROUGH FcLoop (FcLoop doesn't mask it).
 */
class FcLoopTest {

    @Test
    fun `runTurn streams the scripted deltas in order`() = runTest {
        val script = listOf("Hel", "lo", ", ", "world")
        val llm = FakeLocalLlm(responseChunks = script)
        llm.load(File("/tmp/fake-model.litertlm"))
        val loop = FcLoop(llm)

        val emitted = loop.runTurn(
            persona = "You are helpful.",
            history = emptyList(),
            userMessage = "hi",
        ).toList()

        assertEquals("runTurn streams the model deltas verbatim, in order", script, emitted)
    }

    @Test
    fun `buildPrompt places persona first, history in order, then the user message and assistant cue`() {
        val loop = FcLoop(FakeLocalLlm())
        val persona = "You are BlackBox, a terse on-device assistant."
        val history = listOf(
            Turn(Role.USER, "What is 2+2?"),
            Turn(Role.ASSISTANT, "4"),
            Turn(Role.USER, "And 3+3?"),
            Turn(Role.ASSISTANT, "6"),
        )

        val prompt = loop.buildPrompt(persona, history, "Thanks, what about 4+4?")

        // Persona is first.
        assertTrue("persona leads the prompt", prompt.startsWith(persona))

        // History appears, in order, with role markers, before the new message.
        val personaEnd = persona.length
        val u1 = prompt.indexOf("User: What is 2+2?")
        val a1 = prompt.indexOf("Assistant: 4")
        val u2 = prompt.indexOf("User: And 3+3?")
        val a2 = prompt.indexOf("Assistant: 6")
        assertTrue("first user turn present", u1 > personaEnd)
        assertTrue("first assistant turn after first user turn", a1 > u1)
        assertTrue("second user turn after first assistant turn", u2 > a1)
        assertTrue("second assistant turn after second user turn", a2 > u2)

        // The new user message comes after all history, with an assistant cue last.
        val newMsg = prompt.indexOf("User: Thanks, what about 4+4?")
        assertTrue("new user message after history", newMsg > a2)
        assertTrue("prompt ends with an assistant cue for the model to continue",
            prompt.trimEnd().endsWith("Assistant:"))
        assertTrue("assistant cue comes after the new user message",
            prompt.lastIndexOf("Assistant:") > newMsg)
    }

    @Test
    fun `runTurn with empty history builds a valid prompt with persona and user message and streams`() = runTest {
        val llm = FakeLocalLlm(scriptFor = { listOf("ok") })
        llm.load(File("/tmp/fake-model.litertlm"))
        val loop = FcLoop(llm)

        val emitted = loop.runTurn(
            persona = "Persona-Line",
            history = emptyList(),
            userMessage = "hi there",
        ).toList()

        assertEquals(listOf("ok"), emitted)
        // The fake records the prompt it generated against; assert structure on it.
        val prompt = llm.lastPrompt!!
        assertTrue("persona leads", prompt.startsWith("Persona-Line"))
        assertTrue("user message present", prompt.contains("User: hi there"))
        assertTrue("assistant cue last", prompt.trimEnd().endsWith("Assistant:"))
    }

    @Test
    fun `errors thrown mid-stream propagate through FcLoop after the partial deltas`() = runTest {
        val boom = IllegalStateException("engine exploded mid-generation")
        val partial = mutableListOf<String>()

        // A LocalLlm whose generate emits two chunks then throws.
        val throwingLlm = object : LocalLlm {
            override var isLoaded: Boolean = true
                private set
            override suspend fun load(modelFile: File, delegate: String) { isLoaded = true }
            override fun generate(prompt: String): Flow<String> = flow {
                emit("partial-1")
                emit("partial-2")
                throw boom
            }
            override fun close() { isLoaded = false }
        }
        val loop = FcLoop(throwingLlm)

        val thrown = runCatching {
            loop.runTurn("p", emptyList(), "go").collect { partial.add(it) }
        }.exceptionOrNull()

        assertEquals("partial deltas reach the collector before the throw",
            listOf("partial-1", "partial-2"), partial)
        assertTrue("the mid-stream error propagates (is not swallowed)", thrown === boom)
    }

    @Test
    fun `a not-loaded engine error surfaces through FcLoop`() = runTest {
        // failIfNotLoaded + no load() — the fake throws when collected.
        val llm = FakeLocalLlm(responseChunks = listOf("never"), failIfNotLoaded = true)
        val loop = FcLoop(llm)

        val thrown = runCatching {
            loop.runTurn("p", emptyList(), "hi").toList()
        }.exceptionOrNull()

        assertTrue(
            "FcLoop must not mask a not-loaded engine; got $thrown",
            thrown is IllegalStateException,
        )
    }

    @Test
    fun `complete collects the stream into the full concatenated reply`() = runTest {
        val llm = FakeLocalLlm(responseChunks = listOf("a", "b", "c"))
        llm.load(File("/tmp/fake-model.litertlm"))
        val loop = FcLoop(llm)

        val full = loop.complete("p", emptyList(), "hi")

        assertEquals("abc", full)
    }
}
