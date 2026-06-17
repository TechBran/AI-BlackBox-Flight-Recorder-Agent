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
 * Offline / graceful-degradation contract (Task 3.4): implementations DEGRADE
 * gracefully when the mesh is unreachable so a single failed tool call never
 * faults the whole agent turn — [execute] returns a `success = false`
 * [ToolResult] (the model verbalizes it) and [searchTools] returns an empty
 * list (the loop surfaces an empty result as graceful "no tools" feedback). The
 * transport failure / non-2xx is caught at the [java.io.IOException] boundary;
 * only genuine faults (e.g. a malformed-body
 * [kotlinx.serialization.SerializationException], which is NOT an IOException)
 * propagate through the loop.
 */
interface ToolBridge {

    /**
     * Discover up to [k] tool schemas matching [query] (operator-agnostic).
     * Returns an empty list on no match OR when the mesh is unreachable — the
     * caller treats an empty result as graceful "no tools available" feedback.
     */
    suspend fun searchTools(query: String, k: Int = 5): List<ToolSchema>

    /**
     * Run [tool] with [params] for [operator], returning its raw [ToolResult].
     * On an unreachable mesh, returns a `success = false` [ToolResult] describing
     * the failure rather than throwing.
     */
    suspend fun execute(
        tool: String,
        params: JsonObject = JsonObject(emptyMap()),
        operator: String,
    ): ToolResult
}
