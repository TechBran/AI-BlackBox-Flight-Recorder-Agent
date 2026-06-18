package com.aiblackbox.portal.data.local

/*
 * Per-turn tool-result context budget (snapshot-ledger Task 9).
 *
 * /local/turn/prepare already bounds the assembled PACKAGE (<= 16000 chars), but
 * within a single native turn the engine-driven loop ([generateWithToolsNative])
 * makes MULTIPLE tool calls and feeds each tool RESULT back into the SAME ~16K
 * window. A turn that fires several tools with big results can still march toward
 * the ceiling (the overflow behind the old "[on-device error]"). These two PURE
 * helpers are the defenses, mirroring the file-scope [overCap] step-cap decision:
 *
 *   1. [trimToolResult] -- HARD trim a single oversized result before it is
 *      re-fed, so one huge result can't blow the window on its own.
 *   2. [overTurnBudget] -- a per-turn CUMULATIVE budget that SOFT-STOPS the loop
 *      (cleanly finishes; never errors) when total tool-result volume gets large.
 *
 * Both are PURE (primitives/Strings only) so they are JVM-unit-testable under
 * JDK 17; the enforcement inside [nativeOpenApiToolFor]'s `execute` is device/
 * compile-verified (the litertlm engine can't run on the host test JVM).
 */

/** Per-individual-tool-result hard trim (chars). A single huge result (e.g. a
 *  screen read or a multi-snapshot search) can't blow the window on its own. */
const val MAX_TOOL_RESULT_CHARS: Int = 4000

/** Per-turn cumulative tool-result budget (chars). When exceeded, the loop
 *  soft-stops. ~40K chars ≈ ~10K tokens of tool output, leaving room for the
 *  package + the model's answer in the 16K window. Tunable (Task 12). */
const val MAX_TURN_TOOL_RESULT_CHARS: Int = 40000

/** The marker appended to a truncated tool result so the model (and a human
 *  reading the ledger) can see the result was cut for the context budget. */
private const val TRIM_MARKER = "\n[…tool result truncated for context budget]"

/** Truncate an oversized tool result, appending a clear marker. Idempotent for
 *  short inputs (returns as-is). NEVER throws. */
fun trimToolResult(result: String, maxChars: Int = MAX_TOOL_RESULT_CHARS): String =
    if (result.length <= maxChars) result
    else result.take(maxChars) + TRIM_MARKER

/** Pure budget decision: have we spent the per-turn tool-result budget?
 *  usedChars = cumulative chars of (trimmed) tool results fed back this turn. */
fun overTurnBudget(usedChars: Int, maxChars: Int = MAX_TURN_TOOL_RESULT_CHARS): Boolean =
    usedChars > maxChars
