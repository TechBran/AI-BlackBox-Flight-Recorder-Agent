package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.Flow
import java.io.File

/**
 * The on-device language-model runtime seam the Phase 2 chat path generates
 * through. A loaded bundle (see [LocalModelManager.installedModels]) is handed to
 * [load], then [generate] streams the model's reply back as incremental text
 * chunks. The UI/FcLoop concatenates those chunks — exactly the way the network
 * SSE path renders tokens — so swapping the remote provider for the on-device
 * one is a same-shaped streaming substitution.
 *
 * **The concrete implementation is deferred (Task 2.6).** The real engine wraps
 * LiteRT-LM + the AI Edge Function Calling SDK, which need Gradle deps that are
 * not in the offline build cache *and* a physical device to run the native
 * delegates. So this file is the interface only; the rest of Phase 2 (FcLoop,
 * persona caching, [com.aiblackbox.portal.ui.chat] wiring, the snapshot queue)
 * is built and unit-tested against [FakeLocalLlm] in the test source set. The
 * LiteRtEngine that implements this against the device runtime lands in Task 2.6.
 *
 * **Text streaming only — no tool modeling here (yet).** Phase 2 is plain text
 * generation; on-device tool/function calling arrives in Phase 3 via the AI Edge
 * Function Calling SDK, which has its own request/turn shape. Folding tools into
 * this contract now would either over-fit it to one provider's FC protocol or
 * grow speculative surface the fake can't meaningfully honour — so [generate]
 * stays a single prompt-in, text-chunks-out stream. Phase 3 will layer tool
 * support on top (e.g. a richer turn type) without disturbing this text seam.
 */
interface LocalLlm {

    /**
     * Load the model bundle at [modelFile] onto the chosen [delegate]
     * (`"cpu"` / `"gpu"` / `"npu"`). Suspending because the concrete engine
     * memory-maps a multi-GB bundle and warms the native runtime off the main
     * thread. Intended to be called once before generating; implementations
     * should be safe to call again (idempotent-ish) — re-loading the same bundle
     * is a no-op or a clean reload, not a leak.
     */
    suspend fun load(modelFile: File, delegate: String = "cpu")

    /** True once a model is loaded and ready to [generate]. */
    val isLoaded: Boolean

    /**
     * Stream the model's response to [prompt] as incremental text chunks.
     *
     * A **cold** Flow: collecting it starts a generation; not collecting does no
     * work. Each emission is a *delta* (the next piece of text), not the running
     * total — collectors concatenate emissions to build the full reply, matching
     * the SSE token-rendering path the rest of the app already uses. The Flow
     * completes when generation finishes.
     */
    fun generate(prompt: String): Flow<String>

    /** Release native/runtime resources. After [close], the engine is unusable. */
    fun close()
}
