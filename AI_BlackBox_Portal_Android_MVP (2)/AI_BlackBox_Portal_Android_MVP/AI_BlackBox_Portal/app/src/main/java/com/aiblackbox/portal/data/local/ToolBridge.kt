package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.serialization.json.JsonObject

/**
 * Testability seam over the two-hop on-device tool bridge — the abstraction
 * [FcLoop.runAgent] depends on so the agent loop can be unit-tested against a
 * fake (matching the repo's existing seam pattern, e.g. [LocalLlm] /
 * [com.aiblackbox.portal.data.local.PersonaSource]). The production
 * implementation is [ToolBridgeClient] (talking to `/local/tools/search` and
 * `/local/tools/execute`).
 *
 * NOTE: there is a separate FILE `data/model/ToolBridge.kt` holding the DTOs
 * ([ToolSchema], [ToolResult]); this interface lives in `data/local` — different
 * package, no type clash.
 *
 * Offline / graceful-degradation handling is a SEPARATE later task: a non-2xx
 * from either endpoint surfaces as the [java.io.IOException] thrown by the
 * underlying client and is allowed to propagate through the loop.
 */
interface ToolBridge {

    /** Discover up to [k] tool schemas matching [query] (operator-agnostic). */
    suspend fun searchTools(query: String, k: Int = 5): List<ToolSchema>

    /** Run [tool] with [params] for [operator], returning its raw [ToolResult]. */
    suspend fun execute(
        tool: String,
        params: JsonObject = JsonObject(emptyMap()),
        operator: String,
    ): ToolResult
}
