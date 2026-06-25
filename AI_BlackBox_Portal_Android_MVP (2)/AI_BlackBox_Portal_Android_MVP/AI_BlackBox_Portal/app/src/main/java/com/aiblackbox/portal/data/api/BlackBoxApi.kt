package com.aiblackbox.portal.data.api

import android.util.Log
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
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

    val streamClient: OkHttpClient = OkHttpClient.Builder()
        .connectTimeout(30, TimeUnit.SECONDS)
        .readTimeout(0, TimeUnit.MILLISECONDS) // No timeout for SSE
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
     * generic "HTTP 400: Bad Request". Best-effort: any read/parse failure or a
     * missing `detail` falls back to "HTTP <code>: <reason>".
     *
     * NOTE: consumes the response body, so call this only on the error path.
     */
    private fun errorFor(response: Response): IOException {
        val fallback = "HTTP ${response.code}: ${response.message}"
        val detail = try {
            val raw = response.body?.string()
            if (raw.isNullOrBlank()) null
            else json.parseToJsonElement(raw).jsonObject["detail"]?.jsonPrimitive?.content
        } catch (_: Exception) {
            null
        }
        return IOException(detail?.takeIf { it.isNotBlank() } ?: fallback)
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

    suspend fun uploadFile(path: String, file: File, fieldName: String = "file"): String {
        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart(
                fieldName,
                file.name,
                file.asRequestBody("application/octet-stream".toMediaType())
            )
            .build()
        val request = buildRequest(path)
            .post(body)
            .build()
        return client.newCall(request).await().use { response ->
            if (!response.isSuccessful) throw IOException("HTTP ${response.code}: ${response.message}")
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
}
