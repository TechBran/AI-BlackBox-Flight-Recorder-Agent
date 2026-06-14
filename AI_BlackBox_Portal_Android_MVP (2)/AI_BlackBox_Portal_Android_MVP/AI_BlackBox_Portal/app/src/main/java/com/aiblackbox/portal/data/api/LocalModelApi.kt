package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.AttestResponse
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalCatalogResponse
import com.aiblackbox.portal.data.model.LocalStatus
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.suspendCancellableCoroutine
import kotlinx.coroutines.withContext
import okhttp3.Call
import okhttp3.Callback
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import okhttp3.Response
import java.io.File
import java.io.FileOutputStream
import java.io.IOException
import kotlin.coroutines.resume
import kotlin.coroutines.resumeWithException

/**
 * Android client for the hub's `local` provider (on-device Gemma) endpoints.
 *
 * Talks to the four backend routes (Orchestrator/routes/local_routes.py):
 *   - GET  /local/models/catalog            → [catalog]
 *   - GET  /local/models/download/{slug}    → [download] (resumable, Range)
 *   - POST /local/device/attest             → [attest]
 *   - GET  /local/device/status?operator=   → [status]
 *
 * Reuses [BlackBoxApi]'s base URL (`getBaseUrl()`) and lenient
 * kotlinx.serialization `json` so the orchestrator host is never hardcoded — it
 * always tracks whatever host BlackBoxApi was configured with.
 *
 * The non-download calls delegate to BlackBoxApi's get/post helpers. download()
 * needs streaming (multi-GB bundles must never load into RAM) plus a Range
 * header for resume, so it drives an OkHttpClient directly — specifically the
 * shared no-read-timeout [BlackBoxApi.streamClient], so a slow link can't trip
 * the standard 120s read timeout mid-download.
 */
class LocalModelApi(private val api: BlackBoxApi) : LocalModelDownloader {

    private val json get() = api.json

    /** GET /local/models/catalog → the list of downloadable bundles. */
    suspend fun catalog(): List<LocalBundle> {
        val body = api.get("/local/models/catalog")
        return json.decodeFromString(LocalCatalogResponse.serializer(), body).bundles
    }

    /**
     * GET /local/models/download/{slug} — stream the bundle to [destFile],
     * resuming from a prior partial download if one exists.
     *
     * Writes to a sibling `<destFile>.part` temp; if that `.part` already holds
     * N bytes, sends `Range: bytes=N-` and APPENDS the 206 remainder, otherwise
     * downloads from 0. On success the `.part` is renamed to [destFile].
     *
     * [onProgress] is called as bytes arrive with (bytesSoFar, totalBytes),
     * where bytesSoFar counts any already-present prefix and totalBytes is the
     * full bundle size derived from Content-Range (resume) or Content-Length +
     * the existing prefix (fresh). totalBytes is `-1` only if the server sends
     * neither header.
     *
     * [onProgress] is invoked on a background (IO) thread — callers must marshal
     * to the main thread before touching any UI / Compose state from it.
     *
     * Returns [Result.success] of [destFile], or [Result.failure] on any IO /
     * HTTP error (the `.part` is left in place so a later call can resume).
     */
    override suspend fun download(
        slug: String,
        destFile: File,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<File> = withContext(Dispatchers.IO) {
        val partFile = File(destFile.parentFile, destFile.name + ".part")
        val existing = if (partFile.exists()) partFile.length() else 0L

        val urlPath = "/local/models/download/$slug"
        val requestBuilder = Request.Builder()
            .url("${api.getBaseUrl()}$urlPath")
            .header("X-BlackBox-Client", "native-android/1.0")
            .get()
        if (existing > 0L) {
            // Open-ended range: resume from the byte after what we already have.
            requestBuilder.header("Range", "bytes=$existing-")
        }

        try {
            // Use the no-read-timeout client: a multi-GB bundle streamed over
            // Tailscale/cellular can legitimately stall >120s between reads, and
            // the shared client's 120s readTimeout would abort it. connect/write
            // timeouts still apply.
            api.streamClient.newCall(requestBuilder.build()).await().use { response ->
                if (!response.isSuccessful) {
                    return@withContext Result.failure(
                        IOException("HTTP ${response.code}: ${response.message}")
                    )
                }

                val responseBody = response.body
                    ?: return@withContext Result.failure(IOException("empty response body"))

                // If we asked to resume but the server ignored the Range (200,
                // not 206), it's sending the whole file from 0 — start fresh so
                // we don't corrupt the output by appending whole-file bytes onto
                // an existing prefix.
                val resuming = existing > 0L && response.code == 206
                if (existing > 0L && !resuming) {
                    partFile.delete()
                }
                val startedFrom = if (resuming) existing else 0L

                val totalBytes = computeTotal(response, startedFrom, responseBody.contentLength())

                // Emit an initial progress tick so a resumed download reports the
                // prefix immediately (and a 0-byte fresh one reports 0).
                onProgress(startedFrom, totalBytes)

                var written = startedFrom
                responseBody.byteStream().use { input ->
                    FileOutputStream(partFile, /* append = */ resuming).use { output ->
                        val buffer = ByteArray(64 * 1024)
                        while (true) {
                            val n = input.read(buffer)
                            if (n == -1) break
                            output.write(buffer, 0, n)
                            written += n
                            onProgress(written, totalBytes)
                        }
                        output.flush()
                    }
                }

                // Atomic-ish handoff: a fully-written .part becomes the dest.
                if (destFile.exists()) destFile.delete()
                if (!partFile.renameTo(destFile)) {
                    return@withContext Result.failure(
                        IOException("failed to rename ${partFile.name} -> ${destFile.name}")
                    )
                }
                Result.success(destFile)
            }
        } catch (e: IOException) {
            Result.failure(e)
        }
    }

    /** POST /local/device/attest → record a verified on-device model. */
    override suspend fun attest(req: AttestRequest): Boolean {
        return try {
            val body = json.encodeToString(AttestRequest.serializer(), req)
            val responseText = api.post("/local/device/attest", body)
            json.decodeFromString(AttestResponse.serializer(), responseText).success
        } catch (e: IOException) {
            // A 4xx/5xx from the backend (e.g. 400 "operator required") surfaces
            // as an IOException out of BlackBoxApi.post — that's a failed
            // attestation, not a crash.
            false
        }
    }

    /** GET /local/device/status?operator=… → availability + attested models. */
    suspend fun status(operator: String): LocalStatus {
        val url = "${api.getBaseUrl()}/local/device/status".toHttpUrl().newBuilder()
            .addQueryParameter("operator", operator)
            .build()
        // Path + query → reuse BlackBoxApi.get via the relative form so the
        // X-BlackBox-Client header and shared client are applied.
        val path = url.encodedPath + "?" + (url.encodedQuery ?: "")
        val body = api.get(path)
        return json.decodeFromString(LocalStatus.serializer(), body)
    }

    /**
     * Total size of the bundle being downloaded.
     *  - 206 resume: parse the "/<total>" suffix of Content-Range.
     *  - 200 fresh:  Content-Length is the whole file (startedFrom == 0).
     *  - neither:    -1 (unknown).
     */
    private fun computeTotal(response: Response, startedFrom: Long, contentLength: Long): Long {
        val contentRange = response.header("Content-Range")
        if (contentRange != null) {
            val total = contentRange.substringAfterLast('/', "").trim()
            total.toLongOrNull()?.let { if (it >= 0) return it }
        }
        if (contentLength >= 0) return startedFrom + contentLength
        return -1L
    }

    /** Bridge an OkHttp [Call] into a coroutine (mirrors BlackBoxApi.await). */
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
