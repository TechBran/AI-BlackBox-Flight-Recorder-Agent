package com.aiblackbox.portal.data.api

import android.util.Log
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonPrimitive
import okhttp3.Call
import okhttp3.Callback
import okhttp3.Response
import java.io.BufferedReader
import java.io.IOException
import java.io.InputStreamReader

private const val TAG = "SSEClient"

data class SSEEvent(
    val event: String = "message",
    val data: String = "",
    val id: String? = null
)

/**
 * The SSE wire format, line by line, as the box writes it (`event:` / `data:` frames, a
 * blank line ending each, `:` comment keepalives): ONE parser for [SSEClient.stream],
 * [SSEClient.streamGet] and the tests that replay recorded streams (rank 76; the two
 * stream calls each carried their own copy). Each finished frame goes to [emit] with its
 * data decoded ([decodeJsonData]).
 */
class SseFrameParser(private val emit: (SSEEvent) -> Unit) {
    private var eventType = "message"
    private val data = StringBuilder()

    fun line(line: String) {
        when {
            // SSE comment/keepalive frame (": ..."): tolerated and dropped, never data.
            // Explicit branch (M7.1a) so a server-side heartbeat comment can't be
            // misparsed if branches are reordered later.
            line.startsWith(":") -> { /* keepalive — ignore */ }
            line.startsWith("event:") -> eventType = line.removePrefix("event:").trim()
            line.startsWith("data:") -> data.append(line.removePrefix("data:").trim())
            line.isBlank() && data.isNotEmpty() -> flush()
        }
    }

    /** End of stream: a last frame without its trailing blank line still counts. */
    fun finish() {
        if (data.isNotEmpty()) flush()
    }

    private fun flush() {
        emit(SSEEvent(event = eventType, data = decodeJsonData(data.toString())))
        eventType = "message"
        data.clear()
    }

    companion object {
        private val json = Json

        /** Every frame of a whole recorded stream [text]. */
        fun parse(text: String): List<SSEEvent> {
            val out = mutableListOf<SSEEvent>()
            val parser = SseFrameParser { out += it }
            text.lineSequence().forEach(parser::line)
            parser.finish()
            return out
        }

        /**
         * Strip JSON string encoding from SSE data.
         * The backend wraps ALL data values in json.dumps(), so string content
         * arrives as: "Hello world" (with literal quotes). JSON objects arrive
         * as: {"key":"value"} (no outer quotes). This strips the outer quotes
         * from JSON-encoded strings while leaving objects/arrays intact, and
         * also unescapes JSON escape sequences like \n, \t, \\, \", \uXXXX.
         */
        fun decodeJsonData(raw: String): String {
            val trimmed = raw.trim()
            if (trimmed.length >= 2 && trimmed.startsWith("\"") && trimmed.endsWith("\"")) {
                // JSON-encoded string — strip outer quotes and unescape via a proper JSON
                // parser (kotlinx; the same result org.json gave, and it runs on the JVM).
                return try {
                    (json.parseToJsonElement(trimmed) as JsonPrimitive).content
                } catch (_: Exception) {
                    // Fallback: manual unescape if JSON parsing fails
                    trimmed.substring(1, trimmed.length - 1)
                        .replace("\\n", "\n")
                        .replace("\\t", "\t")
                        .replace("\\\\", "\\")
                        .replace("\\\"", "\"")
                }
            }
            // JSON object/array or unquoted value — return as-is
            return trimmed
        }
    }
}

class SSEClient(private val api: BlackBoxApi) {

    /**
     * GET-based SSE stream. Mirrors stream() but uses a GET request — needed
     * for endpoints like /update/log/stream where the task_id is a query
     * parameter and there's no request body.
     */
    fun streamGet(path: String, queryParams: Map<String, String> = emptyMap()): Flow<SSEEvent> = callbackFlow {
        Log.d(TAG, "Starting SSE stream GET to $path")
        val call = api.streamGet(path, queryParams)
        call.enqueue(sseCallback(this::trySend, this::close))
        awaitClose {
            Log.d(TAG, "SSE GET flow cancelled, cancelling HTTP call")
            call.cancel()
        }
    }

    fun stream(path: String, body: String): Flow<SSEEvent> = callbackFlow {
        Log.d(TAG, "Starting SSE stream POST to $path")
        // Lengths only (M7): the body is the person's prompt and the stream their reply, which
        // can quote their health numbers; logcat keeps neither.
        Log.d(TAG, "Request body: ${body.length} chars")
        val call = api.streamPost(path, body)
        call.enqueue(sseCallback(this::trySend, this::close))
        awaitClose {
            Log.d(TAG, "SSE flow cancelled, cancelling HTTP call")
            call.cancel()
        }
    }

    /**
     * The callback both calls share: a non-2xx or bodyless response closes the flow with
     * an [IOException]; a 2xx body is read line by line through [SseFrameParser] and each
     * frame sent on.
     */
    private fun sseCallback(
        send: (SSEEvent) -> Any,
        closeWith: (Throwable?) -> Boolean,
    ): Callback = object : Callback {
        override fun onResponse(call: Call, response: Response) {
            Log.d(TAG, "SSE response code: ${response.code}, content-type: ${response.header("Content-Type")}")
            if (!response.isSuccessful) {
                val errorBody = try { response.body?.string()?.take(500) } catch (_: Exception) { "unreadable" }
                Log.e(TAG, "SSE HTTP error ${response.code}: $errorBody")
                closeWith(IOException("SSE HTTP ${response.code}: ${response.message}"))
                return
            }
            try {
                val body = response.body
                if (body == null) {
                    Log.e(TAG, "SSE response body is null")
                    closeWith(IOException("SSE response body is null"))
                    return
                }
                val parser = SseFrameParser { event ->
                    Log.d(TAG, "SSE event: type=${event.event}, data=${event.data.length} chars")
                    send(event)
                }
                BufferedReader(InputStreamReader(body.byteStream())).forEachLine(parser::line)
                parser.finish()
                Log.d(TAG, "SSE stream completed normally")
                closeWith(null)
            } catch (e: Exception) {
                Log.e(TAG, "SSE read error: ${e.message}", e)
                closeWith(e)
            }
        }

        override fun onFailure(call: Call, e: IOException) {
            Log.e(TAG, "SSE connection failed: ${e.message}", e)
            closeWith(e)
        }
    }
}
