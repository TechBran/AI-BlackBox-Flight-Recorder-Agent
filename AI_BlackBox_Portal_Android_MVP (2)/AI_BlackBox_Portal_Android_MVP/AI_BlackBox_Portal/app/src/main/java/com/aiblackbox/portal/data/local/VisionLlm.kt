package com.aiblackbox.portal.data.local

import kotlinx.coroutines.flow.Flow

/**
 * The on-device MULTIMODAL (image-input) generation seam (Task W4). A
 * [LiteRtEngine] implements this IN ADDITION to [LocalLlm] / [ToolCallingLlm] /
 * [NativeToolCallingLlm] when its model bundle accepts image input
 * ([ModelConfig.supportImage]). It streams the model's reply to a prompt that
 * INCLUDES one or more images — used by the direct "look at my screen" path
 * ([com.aiblackbox.portal.ui.chat.ChatViewModel]) for screens the accessibility
 * tree can't read (Compose / WebView / games).
 *
 * **A DIRECT multimodal turn — NOT a tool inside the native agent loop.** The
 * litertlm native tool loop ([NativeToolCallingLlm.generateWithToolsNative]) drives
 * [com.google.ai.edge.litertlm.OpenApiTool.execute], whose return type is `String`
 * — so a `look_at_screen` tool could NOT feed a captured image back into the
 * engine as a tool RESULT. Vision is therefore modeled as its own seam: a single
 * turn whose input contents are `[image…, text]`. (An autonomous "the model gets
 * stuck on a thin accessibility tree and decides to look mid-loop" path is a
 * FUTURE enhancement — it requires a tool-result channel that can carry image
 * bytes back into the engine, which the 0.13.1 `OpenApiTool` String contract does
 * not provide. Out of scope here.)
 *
 * Kept SEPARATE from [LocalLlm.generate] (which stays text-only and frozen) so the
 * working CPU text path is never disturbed by the vision capability.
 */
interface VisionLlm {

    /**
     * Stream the model's reply to [prompt] WITH the given [images] as incremental
     * text chunks (a delta stream, identical in shape to [LocalLlm.generate]).
     *
     * The [images] are PNG-encoded byte arrays (e.g. from
     * [com.aiblackbox.portal.overlay.ScreenCapture]); the implementation orders
     * them BEFORE the text in the model's contents (Edge Gallery's
     * `runInference(images)` ordering). A **cold** Flow: collecting it starts a
     * generation. Implementations REQUIRE a vision-capable model and a non-empty
     * [images] list — a text-only bundle or an empty list throws rather than
     * silently degrading to a text turn (the caller checks capability first and
     * falls back with a clear message). The image bytes are EPHEMERAL: they are
     * used only to build the prompt and MUST NOT be written to the ledger /
     * snapshot transcript.
     */
    fun generateWithImage(prompt: String, images: List<ByteArray>): Flow<String>
}
