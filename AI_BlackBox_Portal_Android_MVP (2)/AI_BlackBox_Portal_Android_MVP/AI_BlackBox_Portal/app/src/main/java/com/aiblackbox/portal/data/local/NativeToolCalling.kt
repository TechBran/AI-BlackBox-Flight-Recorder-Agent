package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.ToolResult
import com.aiblackbox.portal.data.model.ToolSchema
import kotlinx.coroutines.flow.Flow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.add
import kotlinx.serialization.json.buildJsonArray
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.put

/**
 * One tool offered to the litertlm ENGINE-DRIVEN (native) tool loop (Task W3).
 *
 * Unlike the manual path ([ToolCallingLlm.generateWithTools] + [FcLoop.runAgent],
 * which sets `automaticToolCalling = false` and dispatches tool calls itself), the
 * native path sets `automaticToolCalling = true` so the engine calls each tool's
 * [execute] ITSELF, feeds the returned String back into the model, loops until a
 * final answer, then signals done. This fixes the loop-repeat the manual path hit
 * with the small E4B model (we fed tool results as plain text + re-advertised the
 * tools, so the model never saw a clean "done" and re-ran the same call).
 *
 * @property schema the tool's [ToolSchema] (name/description/parameters) - the
 *   model sees its OpenAPI function declaration via [openApiToolFor]'s description.
 * @property execute the SYNCHRONOUS body the engine invokes when the model calls
 *   this tool. It receives the model-supplied arguments as a JSON string and must
 *   return the result as a JSON string (Edge Gallery's
 *   `{"status":"succeeded"|"failed", "result"/"error":...}` shape; build it with
 *   [toResultJsonString]). It is called on the ENGINE's thread; a suspend dispatch
 *   (e.g. [PhoneController.dispatch]) is bridged with `runBlocking(Dispatchers.IO)`
 *   by the caller - exactly as Edge Gallery's `AgentTools.execute` does. The
 *   autonomy gate + credential handoff stay INSIDE that dispatch (the actuator),
 *   so they still fire on this path.
 */
data class NativeTool(
    val schema: ToolSchema,
    val execute: (argsJson: String) -> String,
)

/**
 * The ENGINE-DRIVEN tool-calling seam (Task W3). A [LiteRtEngine] implements this
 * IN ADDITION to [LocalLlm] / [ToolCallingLlm]: the native path runs the litertlm
 * built-in auto tool loop (`automaticToolCalling = true`) so the ENGINE drives the
 * loop and terminates cleanly (`onDone`), instead of [FcLoop] driving it manually.
 *
 * For THIS increment only the resident phone/intent tools run native (cloud
 * `search_tools` discovery stays on the manual [ToolCallingLlm] path); a follow-up
 * unifies them. The selector ([com.aiblackbox.portal.ui.chat.ChatViewModel]) only
 * routes here when the provider `is NativeToolCallingLlm`, so the test fakes (which
 * implement only [ToolCallingLlm]) keep the manual path and nothing regresses.
 */
interface NativeToolCallingLlm {

    /**
     * Run ONE engine-driven agent turn over [tools]: the engine calls
     * [NativeTool.execute] itself for each model tool call, loops, and completes
     * the returned cold [Flow] when the model emits its final answer (`onDone`).
     *
     * Emits [LlmEvent.TextDelta] for streamed assistant text, and - for inline
     * rendering parity with the manual path - [LlmEvent.ToolCall] before each tool
     * runs and [LlmEvent.ToolOutcome] after (bridged out of the engine's
     * synchronous `execute` into the Flow). Faults (`onError`) propagate to the
     * collector; cancellation aborts the in-flight native generation.
     */
    fun generateWithToolsNative(prompt: String, tools: List<NativeTool>): Flow<LlmEvent>
}

/**
 * Thin adapter: serialize this dispatched [ToolResult] to the JSON string a
 * [NativeTool.execute] returns to the litertlm engine (Edge Gallery's
 * `{"status":...,"result"/"error":...}` shape). Delegates to the pure, testable
 * [toResultJsonString].
 */
fun ToolResult.toResultJsonString(): String = toResultJsonString(success, result)

/**
 * Format the [ToolSchema] matches a `search_cloud_tools` call returned into the
 * compact JSON STRING the native engine feeds back to the model (Task W3
 * follow-up). The string is the SUCCESS payload of a Gallery-shaped result built
 * by the caller; it lists each match's `name` + `description` so the model can pick
 * one and call `call_cloud_tool`. Empty input -> `[]` (the caller surfaces "no
 * match / possibly offline" as a failed result). PURE (no litertlm types) so it is
 * JDK17-unit-testable; the per-tool `parameters` schema is intentionally OMITTED
 * here to keep the discovery payload small (the model only needs the name to call).
 */
fun formatCloudToolMatches(tools: List<ToolSchema>): String {
    val arr = buildJsonArray {
        for (t in tools) {
            add(
                buildJsonObject {
                    put("name", t.name)
                    put("description", t.description)
                },
            )
        }
    }
    return cloudMatchesJson.encodeToString(JsonArray.serializer(), arr)
}

/** Local lenient JSON used by [formatCloudToolMatches] (file-scoped; the
 *  same-named [mapperJson] in LiteRtEngine.kt is private to that file). */
private val cloudMatchesJson = Json { ignoreUnknownKeys = true }
