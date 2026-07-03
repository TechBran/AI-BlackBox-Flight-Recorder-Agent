package com.aiblackbox.portal.data.api

import android.util.Log
import kotlinx.coroutines.channels.awaitClose
import kotlinx.coroutines.flow.Flow
import kotlinx.coroutines.flow.callbackFlow
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

class SSEClient(private val api: BlackBoxApi) {

    /**
     * Strip JSON string encoding from SSE data.
     * The backend wraps ALL data values in json.dumps(), so string content
     * arrives as: "Hello world" (with literal quotes). JSON objects arrive
     * as: {"key":"value"} (no outer quotes). This strips the outer quotes
     * from JSON-encoded strings while leaving objects/arrays intact, and
     * also unescapes common JSON escape sequences like \n, \t, \\, \".
     */
    private fun decodeJsonData(raw: String): String {
        val trimmed = raw.trim()
        if (trimmed.length >= 2 && trimmed.startsWith("\"") && trimmed.endsWith("\"")) {
            // JSON-encoded string — strip outer quotes and unescape via proper JSON parser
            // This handles \n, \t, \\, \", AND \uXXXX unicode escapes correctly
            return try {
                org.json.JSONObject("{\"v\":${trimmed}}").getString("v")
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

    /**
     * Shared callback that parses the SSE wire format and routes events into
     * the given sender. Extracted so stream() and streamGet() share the
     * frame-decoding logic instead of duplicating ~50 lines apiece.
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
                val reader = BufferedReader(InputStreamReader(body.byteStream()))
                var eventType = "message"
                val data = StringBuilder()
                reader.forEachLine { line ->
                    when {
                        // SSE comment/keepalive frame (": ..."): tolerated and dropped,
                        // never data. Explicit branch (M7.1a) so a server-side heartbeat
                        // comment can't be misparsed if branches are reordered later.
                        line.startsWith(":") -> { /* keepalive — ignore */ }
                        line.startsWith("event:") -> eventType = line.removePrefix("event:").trim()
                        line.startsWith("data:") -> data.append(line.removePrefix("data:").trim())
                        line.isBlank() && data.isNotEmpty() -> {
                            val decoded = decodeJsonData(data.toString())
                            send(SSEEvent(event = eventType, data = decoded))
                            eventType = "message"
                            data.clear()
                        }
                    }
                }
                if (data.isNotEmpty()) {
                    val decoded = decodeJsonData(data.toString())
                    send(SSEEvent(event = eventType, data = decoded))
                }
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

    fun stream(path: String, body: String): Flow<SSEEvent> = callbackFlow {
        Log.d(TAG, "Starting SSE stream POST to $path")
        Log.d(TAG, "Request body: ${body.take(500)}")
        val call = api.streamPost(path, body)
        call.enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                Log.d(TAG, "SSE response code: ${response.code}, content-type: ${response.header("Content-Type")}")
                if (!response.isSuccessful) {
                    val errorBody = try { response.body?.string()?.take(500) } catch (_: Exception) { "unreadable" }
                    Log.e(TAG, "SSE HTTP error ${response.code}: $errorBody")
                    close(IOException("SSE HTTP ${response.code}: ${response.message}"))
                    return
                }
                try {
                    val body = response.body
                    if (body == null) {
                        Log.e(TAG, "SSE response body is null")
                        close(IOException("SSE response body is null"))
                        return
                    }
                    val reader = BufferedReader(InputStreamReader(body.byteStream()))
                    var eventType = "message"
                    val data = StringBuilder()

                    reader.forEachLine { line ->
                        when {
                            // SSE comment/keepalive frame (": ..."): tolerated and
                            // dropped, never data (M7.1a — mirrors sseCallback above).
                            line.startsWith(":") -> { /* keepalive — ignore */ }
                            line.startsWith("event:") -> {
                                eventType = line.removePrefix("event:").trim()
                            }
                            line.startsWith("data:") -> {
                                val rawData = line.removePrefix("data:").trim()
                                data.append(rawData)
                            }
                            line.isBlank() && data.isNotEmpty() -> {
                                val decoded = decodeJsonData(data.toString())
                                Log.d(TAG, "SSE event: type=$eventType, data=${decoded.take(120)}")
                                trySend(SSEEvent(event = eventType, data = decoded))
                                eventType = "message"
                                data.clear()
                            }
                        }
                    }

                    // Flush any remaining data without a trailing blank line
                    if (data.isNotEmpty()) {
                        val decoded = decodeJsonData(data.toString())
                        Log.d(TAG, "SSE flush: type=$eventType, data=${decoded.take(120)}")
                        trySend(SSEEvent(event = eventType, data = decoded))
                    }
                    Log.d(TAG, "SSE stream completed normally")
                    close()
                } catch (e: Exception) {
                    Log.e(TAG, "SSE read error: ${e.message}", e)
                    close(e)
                }
            }

            override fun onFailure(call: Call, e: IOException) {
                Log.e(TAG, "SSE connection failed: ${e.message}", e)
                close(e)
            }
        })

        awaitClose {
            Log.d(TAG, "SSE flow cancelled, cancelling HTTP call")
            call.cancel()
        }
    }
}
