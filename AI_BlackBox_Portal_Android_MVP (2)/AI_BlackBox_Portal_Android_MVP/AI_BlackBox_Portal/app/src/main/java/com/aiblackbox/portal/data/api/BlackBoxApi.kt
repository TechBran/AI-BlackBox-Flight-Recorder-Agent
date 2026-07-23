package com.aiblackbox.portal.data.api

import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.jsonObject
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import java.io.File
import java.io.IOException
import java.util.concurrent.TimeUnit
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

private const val TAG = "BlackBoxApi"

/**
 * A non-2xx HTTP response whose [message] is user-presentable: either the
 * backend's FastAPI `detail` string (customer-facing copy, e.g. the CLI
 * Agent 409 session-cap message) or the "HTTP <code>: <reason>" status
 * line. Subclasses [IOException] so existing `catch (e: IOException)`
 * sites keep working; catch THIS first when a caller wants to show the
 * message verbatim but wrap raw transport failures (whose messages are
 * gibberish like "timeout" or "Failed to connect...") in friendlier copy.
 */
class ApiHttpException(message: String) : IOException(message)

class BlackBoxApi(private val baseUrl: String) {

    val json: Json = Json {
        ignoreUnknownKeys = true
        isLenient = true
        encodeDefaults = true
    }

    private val client: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(120, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    /**
     * SSE transport client (M7.1a — long-TTFB hardening, 2026-07-02).
     *
     * Read timeout is a BOUNDED [STREAM_READ_TIMEOUT_SECONDS] (300s), replacing the
     * previous `readTimeout(0)` (infinite). Why bounded, and why this value:
     *
     *  - The historical failure (2026-04-25) was OkHttp's 10s DEFAULT read timeout
     *    killing `/chat/stream` mid-TTFB — Opus stalled 30-60s before the first
     *    token at ~210k-char prefill. The M3 provider-window audit re-measured the
     *    cold 210k TTFB at 14.0s (claude-opus-4-8) and mandates hardening for the
     *    historical 30-60s band, not the 14s point sample.
     *  - `readTimeout(0)` (the interim fix) tolerates any TTFB but detects NOTHING:
     *    a silently dead TCP path (WiFi walk-out, NAT expiry — no FIN/RST) leaves
     *    the chat stuck in STREAMING forever. There is no other stall watchdog in
     *    the chat pipeline, so the read timeout is the ONLY dead-transport detector.
     *  - 300s = 5-10x the historical stall band, 21x the measured cold TTFB, and
     *    aligned with the orchestrator's own provider-leg read timeout (httpx
     *    timeout=300 in chat_routes) — the client never gives up before the server
     *    leg would. Mid-stream silences are also bounded by this window; the server
     *    emits tool_start/tool_executing before tool runs, per-step heartbeats on
     *    CU sessions, and per-iteration heartbeats on the gemini tool loop, each of
     *    which resets the read timer.
     *  - Connect stays tight ([STREAM_CONNECT_TIMEOUT_SECONDS], 10s): it bounds only
     *    the TCP+TLS handshake, so an unreachable host fails fast instead of
     *    inheriting the wide streaming window.
     *  - callTimeout is deliberately UNSET (0): it would bound the total stream
     *    duration end-to-end, which is unbounded by design for long generations.
     *
     * Scoped HERE, not on [client]: plain request/response calls keep their tight
     * 120s read timeout — only SSE calls get the long-TTFB window.
     */
    val streamClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(STREAM_CONNECT_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .readTimeout(STREAM_READ_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .writeTimeout(STREAM_WRITE_TIMEOUT_SECONDS, TimeUnit.SECONDS)
        .build()

    /**
     * On-box (qwen) TTS client. `/tts/batch` buffers its WAV and flushes response
     * headers only AFTER the whole synth finishes (StreamingResponse(iter([combined]))
     * server-side), so a multi-chunk clip on the one GPU can take far longer than the
     * plain [client]'s 120s read timeout before the first response byte arrives —
     * OkHttp then throws SocketTimeoutException at readResponseHeaders. Give TTS a
     * bounded 300s read window (aligned with the server's per-synth QWEN_TTS_TIMEOUT).
     * STOPGAP: the robust fix is the async TTS task-queue (submit -> poll), which makes
     * this a short request regardless of clip length.
     */
    private val ttsClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(300, TimeUnit.SECONDS)
        .writeTimeout(60, TimeUnit.SECONDS)
        .build()

    private val jsonMediaType = "application/json; charset=utf-8".toMediaType()

    private fun buildRequest(path: String): Request.Builder =
        Request.Builder()
            .url("$baseUrl$path")
            .header("X-BlackBox-Client", "native-android/1.0")

    /**
     * Build an [IOException] for a non-2xx response, preferring the backend's
     * FastAPI `detail` field over the bare HTTP status line (M2a fix). The
     * backend raises HTTPException(detail="…") for validation errors (e.g.
     * "delivery_target (E.164) is required for sms delivery") — surfacing that
     * string lets the cron create/edit dialog show a real reason instead of a
     * generic "HTTP 400: Bad Request". Non-string `detail` (e.g. FastAPI 422
     * validation-error arrays) is stringified as raw JSON rather than dropped.
     * Best-effort: any read/parse failure or a missing `detail` falls back to
     * "HTTP <code>: <reason>".
     *
     * NOTE: consumes the response body, so call this only on the error path.
     *
     * Returns [ApiHttpException] (an [IOException] subtype) so callers can
     * distinguish "server said no, with a presentable message" from raw
     * transport failures (ConnectException, SocketTimeoutException, …).
     */
    private fun errorFor(response: Response): ApiHttpException {
        val fallback = "HTTP ${response.code}: ${response.message}"
        val detail = try {
            val raw = response.body?.string()
            if (raw.isNullOrBlank()) null
            else when (val d = json.parseToJsonElement(raw).jsonObject["detail"]) {
                null, is JsonNull -> null
                is JsonPrimitive -> d.content
                // FastAPI 422 validation errors ship `detail` as an ARRAY of
                // {loc, msg, type} objects — stringify so the reason survives.
                else -> d.toString()
            }
        } catch (_: Exception) {
            null
        }
        return ApiHttpException(detail?.takeIf { it.isNotBlank() } ?: fallback)
    }

    suspend fun get(path: String): String {
        val request = buildRequest(path).get().build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw errorFor(response)
            response.body?.string() ?: ""
        }
    }

    suspend fun post(path: String, body: String): String {
        val request = buildRequest(path)
            .post(body.toRequestBody(jsonMediaType))
            .build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw errorFor(response)
            response.body?.string() ?: ""
        }
    }

    suspend fun put(path: String, body: String): String {
        val request = buildRequest(path)
            .put(body.toRequestBody(jsonMediaType))
            .build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw errorFor(response)
            response.body?.string() ?: ""
        }
    }

    suspend fun delete(path: String): String {
        val request = buildRequest(path).delete().build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw errorFor(response)
            response.body?.string() ?: ""
        }
    }

    /**
     * Multipart file upload. [fields] entries become plain-text form parts
     * (e.g. `session_name` for `/cli-agent/zellij/attach-file`), added BEFORE
     * the file part so stream-parsing servers see the metadata first; the
     * default (empty) leaves the single-file wire format unchanged for
     * existing call sites. Non-2xx responses throw [ApiHttpException] via
     * [errorFor], surfacing FastAPI's JSON `detail` (stringified when
     * non-string) instead of a bare "HTTP <code>" status line.
     */
    suspend fun uploadFile(
        path: String,
        file: File,
        fieldName: String = "file",
        fields: Map<String, String> = emptyMap(),
    ): String {
        val builder = MultipartBody.Builder().setType(MultipartBody.FORM)
        fields.forEach { (key, value) -> builder.addFormDataPart(key, value) }
        builder.addFormDataPart(
            fieldName,
            file.name,
            file.asRequestBody("application/octet-stream".toMediaType())
        )
        val request = buildRequest(path)
            .post(builder.build())
            .build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw errorFor(response)
            response.body?.string() ?: ""
        }
    }

    /** Fetch raw bytes (for images/binary). Returns null on failure. */
    suspend fun getBytes(path: String): ByteArray? {
        val request = buildRequest(path).get().build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) return@use null
            response.body?.bytes()
        }
    }

    fun streamPost(path: String, body: String): Call {
        Log.d(TAG, "streamPost: ${baseUrl}${path}")
        val request = buildRequest(path)
            .post(body.toRequestBody(jsonMediaType))
            .build()
        return streamClient.newCall(request)
    }

    fun streamGet(path: String, queryParams: Map<String, String> = emptyMap()): Call {
        val urlBuilder = "$baseUrl$path".toHttpUrl().newBuilder()
        queryParams.forEach { (key, value) -> urlBuilder.addQueryParameter(key, value) }
        val request = Request.Builder()
            .url(urlBuilder.build())
            .header("X-BlackBox-Client", "native-android/1.0")
            .get()
            .build()
        return streamClient.newCall(request)
    }

    fun getClient(): OkHttpClient = client

    /** On-box TTS client (300s read window — see [ttsClient]). Use for /tts/batch. */
    fun getTtsClient(): OkHttpClient = ttsClient

    fun getBaseUrl(): String = baseUrl

    private suspend fun Call.await(): Response = suspendCancellableCoroutine { cont ->
        cont.invokeOnCancellation { cancel() }
        enqueue(object : Callback {
            override fun onResponse(call: Call, response: Response) {
                cont.resume(response)
            }

            override fun onFailure(call: Call, e: IOException) {
                if (cont.isCancelled) return
                cont.resumeWithException(e)
            }
        })
    }

    companion object {
        /**
         * SSE stream transport timeouts (M7.1a). See [streamClient] KDoc for the
         * full rationale; pinned by BlackBoxApiStreamTimeoutTest so a refactor
         * cannot silently reintroduce the 10s-default (2026-04-25 stall) or the
         * infinite-hang (readTimeout 0) failure modes.
         */
        const val STREAM_CONNECT_TIMEOUT_SECONDS = 10L
        const val STREAM_READ_TIMEOUT_SECONDS = 300L
        const val STREAM_WRITE_TIMEOUT_SECONDS = 60L
    }
}
