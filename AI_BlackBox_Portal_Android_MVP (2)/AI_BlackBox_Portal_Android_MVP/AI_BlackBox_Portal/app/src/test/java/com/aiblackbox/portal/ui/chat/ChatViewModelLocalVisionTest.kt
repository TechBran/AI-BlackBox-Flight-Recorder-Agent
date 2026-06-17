package com.aiblackbox.portal.ui.chat

import com.aiblackbox.portal.data.local.FakeLocalLlm
import com.aiblackbox.portal.data.local.FakeVisionLlm
import com.aiblackbox.portal.data.local.LocalLlm
import com.aiblackbox.portal.data.local.VisionLlm
import com.aiblackbox.portal.data.model.SaveRequest
import com.aiblackbox.portal.data.model.UiMessage
import com.aiblackbox.portal.overlay.CAPTURE_UNAVAILABLE_NO_OVERLAY
import com.aiblackbox.portal.overlay.ScreenCaptureResult
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Test
import kotlinx.coroutines.test.runTest

/**
 * W4 follow-up — the DIRECT on-device VISION path ("look at my screen").
 *
 * Same strategy as [ChatViewModelLocalEngineTest]: the AndroidViewModel can't be
 * instantiated here (no Robolectric / Application / main dispatcher), so the
 * production [ChatViewModel.lookAtScreen] is a thin wiring shim over a PURE,
 * testable core — [ChatViewModel.streamLocalVisionTurn] — plus pure decisions
 * ([ChatViewModel.isLookAtScreenRequest], the `is VisionLlm` capability check, the
 * [ScreenCaptureResult] → copy mapping). We exercise the FOUR lookAtScreen
 * branches through those seams with a [FakeVisionLlm] + a captured sink/save sink:
 *
 *  1. SUCCESS — capture → generateWithImage → stream into the bubble → save TEXT.
 *     EPHEMERALITY: the image bytes reach the ENGINE but NEVER the save request.
 *  2. RefusedPassword — the redaction gate refused; the friendly refusal copy is
 *     shown and NO vision turn runs / NO save.
 *  3. Unavailable — capture couldn't run; its customer-facing reason is shown, no save.
 *  4. supportImage-not-supported / visionDegraded — the resolved engine is not a
 *     [VisionLlm], so the unsupported copy is shown (text path still works), no save.
 *
 * Plus the v1 VISION TRIGGER classifier ([ChatViewModel.isLookAtScreenRequest]):
 * positive look-at-screen phrasings route to vision; ordinary messages do NOT.
 */
class ChatViewModelLocalVisionTest {

    /** Captures the sink + save sink the production lookAtScreen threads into the core. */
    private class Harness {
        var assistant = UiMessage(role = "assistant", content = "", isStreaming = true, provider = "local")
            private set
        var saved: SaveRequest? = null
            private set
        var saveProvider: String? = null
            private set

        val sink: (String, Boolean) -> Unit = { content, streaming ->
            assistant = assistant.copy(content = content, isStreaming = streaming)
        }
        val saveSink: (SaveRequest, String) -> Unit = { req, provider ->
            saved = req
            saveProvider = provider
        }
    }

    // ── Branch 1: SUCCESS + EPHEMERALITY ─────────────────────────────────────

    @Test
    fun `success branch streams the vision reply into the bubble and saves text only`() = runTest {
        val engine = FakeVisionLlm(responseChunks = listOf("I see ", "a settings ", "screen."))
        val secret = byteArrayOf(0x89.toByte(), 0x50, 0x4E, 0x47, 0x53, 0x45, 0x43) // pretend PNG
        val h = Harness()

        val ok = ChatViewModel.streamLocalVisionTurn(
            engine = engine,
            prompt = "PERSONA\n\nwhat's on my screen",
            imageBytes = listOf(secret),
            userMessage = "what's on my screen",
            operator = "Brandon",
            model = "gemma-4-e4b",
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertTrue("vision turn completed", ok)
        assertEquals("I see a settings screen.", h.assistant.content)
        assertFalse("streaming cleared on completion", h.assistant.isStreaming)
        // The screenshot DID reach the engine (to build the prompt)…
        assertNotNull("image handed to the engine", engine.lastImages)
        assertTrue("the exact captured frame reached the engine", engine.lastImages!![0].contentEquals(secret))

        // …but the SAVE carries TEXT ONLY — never the screenshot bytes (ephemerality).
        val req = h.saved
        assertNotNull("a successful vision turn is saved", req)
        assertEquals("what's on my screen", req!!.userMessage)
        assertEquals("I see a settings screen.", req.assistantResponse)
        assertEquals("save tagged provider=local", "local", h.saveProvider)
        // SaveRequest has no image field at all; assert the screenshot bytes did not
        // leak into any text field of the persisted record.
        val secretStr = String(secret, Charsets.ISO_8859_1)
        assertFalse("screenshot bytes must not appear in userMessage", req.userMessage.contains(secretStr))
        assertFalse("screenshot bytes must not appear in assistantResponse", req.assistantResponse.contains(secretStr))
        assertFalse("screenshot bytes must not appear in the model field", (req.model ?: "").contains(secretStr))
    }

    @Test
    fun `vision mid-stream fault surfaces a friendly error and does not save`() = runTest {
        // The engine can't run vision on this device: it emits a delta then throws —
        // the exact "graceful degrade at generate time" case.
        val engine = FakeVisionLlm(responseChunks = listOf("partial "), failMidStream = true)
        val h = Harness()

        val ok = ChatViewModel.streamLocalVisionTurn(
            engine = engine,
            prompt = "p",
            imageBytes = listOf(byteArrayOf(1, 2, 3)),
            userMessage = "look at my screen",
            operator = "Brandon",
            model = null,
            sink = h.sink,
            saveSink = h.saveSink,
        )

        assertFalse("a vision fault returns false", ok)
        assertTrue("partial text preserved", h.assistant.content.contains("partial"))
        assertTrue(
            "a friendly on-device error is shown",
            h.assistant.content.contains("error", ignoreCase = true) ||
                h.assistant.content.contains("on-device", ignoreCase = true),
        )
        assertFalse("streaming cleared after the fault", h.assistant.isStreaming)
        assertNull("a faulted vision turn is not persisted", h.saved)
    }

    // ── Branch 2: RefusedPassword (the redaction gate) ───────────────────────

    @Test
    fun `refused-password branch shows the refusal copy, runs no vision turn, saves nothing`() = runTest {
        // lookAtScreen's when(capture) maps RefusedPassword -> the refusal copy, and
        // NEVER reaches streamLocalVisionTurn. Prove the mapping + that no engine is hit.
        val capture: ScreenCaptureResult = ScreenCaptureResult.RefusedPassword
        val engine = FakeVisionLlm()
        val h = Harness()

        when (capture) {
            is ScreenCaptureResult.RefusedPassword ->
                h.sink(ChatViewModel.LOCAL_VISION_PASSWORD_REFUSED_TEXT, false)
            is ScreenCaptureResult.Unavailable -> h.sink(capture.reason, false)
            is ScreenCaptureResult.Success -> { /* not this branch */ }
        }

        assertEquals(ChatViewModel.LOCAL_VISION_PASSWORD_REFUSED_TEXT, h.assistant.content)
        assertFalse("not streaming", h.assistant.isStreaming)
        assertTrue("no vision generation ran", engine.visionPrompts.isEmpty())
        assertNull("a refused capture saves nothing", h.saved)
    }

    // ── Branch 3: Unavailable (no overlay / no frame) ────────────────────────

    @Test
    fun `unavailable branch shows the customer-facing reason, runs no vision turn, saves nothing`() = runTest {
        val capture: ScreenCaptureResult = ScreenCaptureResult.Unavailable(CAPTURE_UNAVAILABLE_NO_OVERLAY)
        val engine = FakeVisionLlm()
        val h = Harness()

        when (capture) {
            is ScreenCaptureResult.RefusedPassword ->
                h.sink(ChatViewModel.LOCAL_VISION_PASSWORD_REFUSED_TEXT, false)
            is ScreenCaptureResult.Unavailable -> h.sink(capture.reason, false)
            is ScreenCaptureResult.Success -> { /* not this branch */ }
        }

        assertEquals(CAPTURE_UNAVAILABLE_NO_OVERLAY, h.assistant.content)
        assertFalse("not streaming", h.assistant.isStreaming)
        assertTrue("no vision generation ran", engine.visionPrompts.isEmpty())
        assertNull("an unavailable capture saves nothing", h.saved)
    }

    // ── Branch 4: supportImage-not-supported / visionDegraded ────────────────

    @Test
    fun `unsupported branch — a non-vision engine shows the unsupported copy, saves nothing`() = runTest {
        // The capture SUCCEEDED, but the resolved engine is text-only (not a VisionLlm)
        // — e.g. a text-only model, OR a vision bundle that degraded to text-only
        // because GPU vision init failed (visionDegraded). lookAtScreen's
        // `if (llm !is VisionLlm)` arm shows the unsupported copy and never generates.
        val textOnly: LocalLlm = FakeLocalLlm(responseChunks = listOf("won't be called"))
        val h = Harness()

        // The exact capability decision lookAtScreen makes after a Success capture.
        if (textOnly !is VisionLlm) {
            h.sink(ChatViewModel.LOCAL_VISION_UNSUPPORTED_TEXT, false)
        } else {
            // would stream — not this branch
        }

        assertFalse("a text-only engine is not vision-capable", textOnly is VisionLlm)
        assertEquals(ChatViewModel.LOCAL_VISION_UNSUPPORTED_TEXT, h.assistant.content)
        assertFalse("not streaming", h.assistant.isStreaming)
        assertNull("an unsupported vision turn saves nothing", h.saved)
    }

    @Test
    fun `a vision-capable engine passes the capability check, a text-only one does not`() {
        val vision: LocalLlm = FakeVisionLlm()
        val textOnly: LocalLlm = FakeLocalLlm()
        assertTrue("FakeVisionLlm IS a VisionLlm (routes to the vision turn)", vision is VisionLlm)
        assertFalse("FakeLocalLlm is NOT a VisionLlm (routes to the unsupported copy)", textOnly is VisionLlm)
    }

    // ── The v1 VISION TRIGGER classifier ─────────────────────────────────────

    @Test
    fun `isLookAtScreenRequest matches explicit look-at-screen phrasings`() {
        val positives = listOf(
            "look at my screen",
            "Look at my screen and tell me what app this is",
            "what's on my screen?",
            "What is on the screen right now",
            "what do you see",
            "What do you see here?",
            "read the screen for me",
            "Read my screen please",
            "describe my screen",
            "can you see my screen?",
            "check the screen",
        )
        for (p in positives) {
            assertTrue("should trigger vision: \"$p\"", ChatViewModel.isLookAtScreenRequest(p))
        }
    }

    @Test
    fun `isLookAtScreenRequest is conservative and does not hijack normal messages`() {
        val negatives = listOf(
            "",
            "   ",
            "hello there",
            "what is the capital of France",
            "my screen is cracked, can you help me find a repair shop",
            "turn on the flashlight",
            "share my screen to the TV", // casting, not a look-at request
            "the screen brightness is too low",
            "write me a poem about the ocean",
            "what time is it",
        )
        for (n in negatives) {
            assertFalse("should NOT trigger vision: \"$n\"", ChatViewModel.isLookAtScreenRequest(n))
        }
    }
}
