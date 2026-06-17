package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.flow
import java.io.File

/**
 * In-test [LocalLlm] double for the Phase 2 layers that generate through the
 * on-device engine (FcLoop, ChatViewModel.sendViaLocalEngine, the snapshot
 * queue). It stands in for the deferred LiteRT-LM engine (Task 2.6) so the whole
 * chat path can be exercised offline, on the JVM, with no AI Edge SDK deps and
 * no device.
 *
 * **Scriptable two ways:**
 *  - [responseChunks] — a fixed list emitted for *any* prompt; or
 *  - [scriptFor] — a `(prompt) -> List<String>` for prompt-dependent replies.
 *
 * Exactly one should be supplied; if both are given, [scriptFor] wins. If
 * neither is given the fake emits nothing (an empty stream).
 *
 * **Records interactions** for assertions: [loadedFile], [loadedDelegate],
 * [loadCount], [prompts], [lastPrompt], [closed], and [isLoaded].
 *
 * It is a `class` (not an `object`) so each test gets a fresh, isolated instance.
 * Lives in the test source set — never shipped.
 *
 * @param failIfNotLoaded when true, collecting [generate] before [load] throws
 *   [IllegalStateException] (matching the contract the concrete engine will
 *   enforce). Default false so simple streaming tests need not call [load].
 */
class FakeLocalLlm(
    private val responseChunks: List<String> = emptyList(),
    private val scriptFor: ((prompt: String) -> List<String>)? = null,
    private val failIfNotLoaded: Boolean = false,
) : LocalLlm {

    /** The file passed to the most recent [load], or null if never loaded. */
    var loadedFile: File? = null
        private set

    /** The delegate passed to the most recent [load], or null if never loaded. */
    var loadedDelegate: String? = null
        private set

    /** How many times [load] was called. */
    var loadCount: Int = 0
        private set

    /** Every prompt passed to [generate], in collection order. */
    val prompts: MutableList<String> = mutableListOf()

    /** The most recent prompt passed to [generate], or null if never called. */
    val lastPrompt: String? get() = prompts.lastOrNull()

    /** True once [close] has been called. */
    var closed: Boolean = false
        private set

    override var isLoaded: Boolean = false
        private set

    override suspend fun load(modelFile: File, delegate: String) {
        loadedFile = modelFile
        loadedDelegate = delegate
        loadCount++
        isLoaded = true
    }

    override fun generate(prompt: String): Flow<String> = flow {
        if (failIfNotLoaded && !isLoaded) {
            error("FakeLocalLlm.generate() called before load()")
        }
        // Record at collection time (cold Flow): the prompt is "used" only when
        // the stream is actually consumed, mirroring real generation.
        prompts.add(prompt)
        val chunks = scriptFor?.invoke(prompt) ?: responseChunks
        for (chunk in chunks) emit(chunk)
    }

    override fun close() {
        closed = true
        isLoaded = false
    }
}
