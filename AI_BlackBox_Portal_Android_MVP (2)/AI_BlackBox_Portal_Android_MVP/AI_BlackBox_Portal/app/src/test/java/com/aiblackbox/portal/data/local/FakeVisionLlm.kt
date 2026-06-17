package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import java.io.File

/**
 * In-test double for the on-device MULTIMODAL engine (Task W4): implements BOTH
 * [LocalLlm] (so `provider()` resolves to a real engine) AND [VisionLlm] (so the
 * `is VisionLlm` capability check in
 * [com.aiblackbox.portal.ui.chat.ChatViewModel.lookAtScreen] passes), exactly like
 * the concrete [LiteRtEngine]. It lets the W4-follow-up orchestration tests
 * exercise [com.aiblackbox.portal.ui.chat.ChatViewModel.streamLocalVisionTurn]
 * offline, on the JVM, with no AI Edge SDK / device.
 *
 * **Scriptable:** [responseChunks] are emitted (as deltas) for any vision turn; if
 * [failMidStream] is true the stream emits the chunks then THROWS (the "engine
 * can't run vision on this device" fault the graceful-degrade path guards against).
 *
 * **Records interactions** for assertions — crucially [lastImages] so a test can
 * prove the captured screenshot bytes reached the ENGINE (prompt-build) but never
 * the save request (ephemerality). Lives in the test source set — never shipped.
 */
class FakeVisionLlm(
    private val responseChunks: List<String> = listOf("I see your screen."),
    private val failMidStream: Boolean = false,
) : LocalLlm, VisionLlm {

    /** Every (prompt) passed to [generateWithImage], in collection order. */
    val visionPrompts: MutableList<String> = mutableListOf()

    /** The most recent prompt passed to [generateWithImage], or null. */
    val lastVisionPrompt: String? get() = visionPrompts.lastOrNull()

    /** The image byte arrays handed to the most recent [generateWithImage] call. */
    var lastImages: List<ByteArray>? = null
        private set

    override var isLoaded: Boolean = true
        private set

    override suspend fun load(modelFile: File, delegate: String) {
        isLoaded = true
    }

    // The text path is unused by the vision orchestration; an empty stream is fine.
    override fun generate(prompt: String): Flow<String> = flow { }

    override fun generateWithImage(prompt: String, images: List<ByteArray>): Flow<String> = flow {
        // Record at collection time (cold Flow), mirroring real generation.
        visionPrompts.add(prompt)
        lastImages = images
        for (chunk in responseChunks) emit(chunk)
        if (failMidStream) throw RuntimeException("vision not supported on this device")
    }

    override fun close() {
        isLoaded = false
    }
}
