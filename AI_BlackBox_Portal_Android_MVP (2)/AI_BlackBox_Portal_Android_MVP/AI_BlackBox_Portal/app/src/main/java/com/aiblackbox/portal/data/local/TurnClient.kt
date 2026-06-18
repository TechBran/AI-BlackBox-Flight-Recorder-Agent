package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.BlackBoxApi
import com.aiblackbox.portal.data.model.CompleteRequest
import com.aiblackbox.portal.data.model.CompleteResponse
import com.aiblackbox.portal.data.model.PrepareRequest
import com.aiblackbox.portal.data.model.PrepareResponse
import java.io.IOException

/**
 * Android client for the two server-bracketed on-device turn endpoints — the hub
 * brackets every on-device Gemma turn so the phone never has to assemble context
 * or persist memory itself (Orchestrator/routes/local_routes.py):
 *   - POST /local/turn/prepare   → [prepare]  (assemble the per-turn package)
 *   - POST /local/turn/complete  → [complete] (persist + mint the finished turn)
 *
 * Mirrors [ToolBridgeClient]: it reuses [BlackBoxApi]'s base URL + lenient
 * kotlinx.serialization `json`, so the orchestrator host is never hardcoded and
 * request bodies are built from `@Serializable` DTOs rather than hand-concatenated
 * strings.
 *
 * **Offline contract.** Both methods return a NULLABLE response, and `null` is the
 * explicit OFFLINE / unreachable signal that Task 11's degraded mode keys off. A
 * transport failure or a non-2xx (both of which [BlackBoxApi.post] raises as an
 * [IOException]) is CAUGHT and turned into `null` rather than thrown. Only
 * [IOException] is caught: a `kotlinx.serialization.SerializationException` (a
 * malformed body) is a real bug, not "offline", and still propagates.
 */
class TurnClient(private val api: BlackBoxApi) {

    private val json get() = api.json

    /**
     * POST /local/turn/prepare — assemble the per-turn package server-side (system
     * prompt + relevant tools + memory provenance + budget) for [prompt] on behalf
     * of [operator], BEFORE the on-device model runs.
     *
     * Returns the parsed [PrepareResponse], or `null` when the BlackBox is
     * unreachable — a transport failure or a non-2xx both surface as an
     * [IOException] from [BlackBoxApi.post], which is CAUGHT here and returned as
     * `null`, the OFFLINE signal Task 11's degraded mode keys off (so the phone can
     * fall back to a local-only turn instead of faulting). A `SerializationException`
     * (a malformed body — a real bug) still propagates.
     */
    suspend fun prepare(prompt: String, operator: String): PrepareResponse? {
        val body = json.encodeToString(
            PrepareRequest.serializer(),
            PrepareRequest(prompt = prompt, operator = operator),
        )
        return try {
            val responseText = api.post("/local/turn/prepare", body)
            json.decodeFromString(PrepareResponse.serializer(), responseText)
        } catch (_: IOException) {
            // Transport failure / non-2xx → the mesh is unreachable. Signal OFFLINE
            // with null instead of faulting the turn.
            null
        }
    }

    /**
     * POST /local/turn/complete — report the finished turn ([req] carries the final
     * answer + tool transcript) so the hub persists the immutable snapshot and may
     * trigger a checkpoint.
     *
     * Returns the parsed [CompleteResponse], or `null` when the BlackBox is
     * unreachable — same offline contract as [prepare]: a transport failure or a
     * non-2xx both surface as an [IOException] from [BlackBoxApi.post], CAUGHT here
     * and returned as `null` (the OFFLINE signal). A `SerializationException` (a
     * malformed body — a real bug) still propagates.
     */
    suspend fun complete(req: CompleteRequest): CompleteResponse? {
        val body = json.encodeToString(CompleteRequest.serializer(), req)
        return try {
            val responseText = api.post("/local/turn/complete", body)
            json.decodeFromString(CompleteResponse.serializer(), responseText)
        } catch (_: IOException) {
            null
        }
    }
}
