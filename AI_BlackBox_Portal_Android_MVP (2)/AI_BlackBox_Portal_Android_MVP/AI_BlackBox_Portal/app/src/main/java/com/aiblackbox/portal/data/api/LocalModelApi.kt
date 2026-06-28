package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.AttestResponse
import com.aiblackbox.portal.data.model.AutonomyRequest
import com.aiblackbox.portal.data.model.AutonomyResponse
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalCatalogResponse
import com.aiblackbox.portal.data.model.LocalStatus
import com.aiblackbox.portal.data.model.PersonaResponse
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
 * Talks to the backend routes (Orchestrator/routes/local_routes.py):
 *   - GET  /local/models/catalog            → [catalog]
 *   - POST /local/device/attest             → [attest]
 *   - GET  /local/device/status?operator=   → [status]
 *
 * [download] no longer hits the hub: bytes stream DIRECTLY from the Hugging Face
 * CDN ([com.aiblackbox.portal.data.model.LocalBundle.downloadUrl]) after the hub
 * byte-proxy was deleted (2026-06-27).
 *
 * Reuses [BlackBoxApi]'s base URL (`getBaseUrl()`) and lenient
 * kotlinx.serialization `json` so the orchestrator host is never hardcoded — it
 * always tracks whatever host BlackBoxApi was configured with.
 *
 * The non-download calls delegate to BlackBoxApi's get/post helpers. download()
 * needs streaming (multi-GB bundles must never load into RAM) plus a Range
 * header for resume, so it drives an OkHttpClient directly — a 90s-read-timeout
 * client derived from [BlackBoxApi.streamClient] so a real stall surfaces as a
 * retryable failure instead of an eternal 0%.
 */
class LocalModelApi(
    private val api: BlackBoxApi,
    // BYOK seam for future gated repos: returns the HF token (or null). Defaulted so
    // existing callers (LocalModelApi(api) / LocalModelApi(BlackBoxApi(baseUrl))) are
    // unaffected; only used when bundle.gated is true. YAGNI stub returns null today.
    private val hfToken: () -> String? = { null },
) : LocalModelDownloader, LocalModelCatalogClient, PersonaSource {

    private val json get() = api.json

    // 90s read timeout: bytes now stream direct from the HF CDN (a steady link), so a
    // real stall must surface as a retryable failure instead of an eternal 0%. Built
    // from the shared streamClient (which has readTimeout 0); connect/write timeouts
    // and the rest of the client config are inherited.
    private val downloadClient = api.streamClient.newBuilder()
        .readTimeout(90, java.util.concurrent.TimeUnit.SECONDS)
        .build()

    /** GET /local/models/catalog → the list of downloadable bundles. */
    override suspend fun catalog(): List<LocalBundle> {
        val body = api.get("/local/models/catalog")
        return json.decodeFromString(LocalCatalogResponse.serializer(), body).bundles
    }

    /**
     * Stream [bundle] to [destFile] DIRECTLY from the Hugging Face CDN
     * ([bundle.downloadUrl]), resuming from a prior partial download if one exists.
     *
     * The bytes no longer flow through the hub byte-proxy (deleted 2026-06-27) — the
     * URL is the bundle's HF `resolve` URL, with a defensive fallback constructed from
     * `hfRepo`/`filename`. Because the request goes to HF (not the hub), the
     * `X-BlackBox-Client` header is NOT sent; a gated repo attaches
     * `Authorization: Bearer <token>` only when the [hfToken] seam returns non-null.
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
        bundle: LocalBundle,
        destFile: File,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<File> = withContext(Dispatchers.IO) {
        val partFile = File(destFile.parentFile, destFile.name + ".part")
        val existing = if (partFile.exists()) partFile.length() else 0L

        val url = bundle.downloadUrl.ifBlank {
            // Defensive fallback: construct the HF resolve URL from coordinates.
            "https://huggingface.co/${bundle.hfRepo}/resolve/main/${bundle.filename}"
        }
        val requestBuilder = Request.Builder()
            .url(url)
            .get()
        if (existing > 0L) {
            // Open-ended range: resume from the byte after what we already have.
            requestBuilder.header("Range", "bytes=$existing-")
        }
        // Gated repos need an HF token; attach only when the BYOK seam provides one.
        // NOTE: no X-BlackBox-Client header — this request goes to HF, not the hub.
        if (bundle.gated) hfToken()?.let { requestBuilder.header("Authorization", "Bearer $it") }

        try {
            // 90s-read-timeout client: a steady CDN stream lets a real stall surface
            // as a retryable failure instead of an eternal 0% (streamClient had
            // readTimeout 0). connect/write timeouts still apply.
            downloadClient.newCall(requestBuilder.build()).await().use { response ->
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

    /**
     * POST /local/device/autonomy → set a device's autonomy posture.
     *
     * Body `{operator, device_id, mode}` where mode is "yolo" (full autonomy)
     * or "permission" (asks before high-consequence phone actions). Mirrors
     * [attest]'s style: encode the request, POST, parse `{success}`; a 4xx/5xx
     * out of [BlackBoxApi.post] surfaces as an IOException → returns false
     * (a rejected change, not a crash).
     */
    override suspend fun setAutonomy(operator: String, deviceId: String, mode: String): Boolean {
        return try {
            val req = AutonomyRequest(operator = operator, deviceId = deviceId, mode = mode)
            val body = json.encodeToString(AutonomyRequest.serializer(), req)
            val responseText = api.post("/local/device/autonomy", body)
            json.decodeFromString(AutonomyResponse.serializer(), responseText).success
        } catch (e: IOException) {
            false
        }
    }

    /** GET /local/device/status?operator=… → availability + attested models. */
    override suspend fun status(operator: String): LocalStatus {
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
     * GET /local/system-prompt?operator=… → the on-device persona / system
     * prompt ({prompt, version}), built server-side from `behavioral_core` so the
     * `local` provider reasons with the SAME persona the cloud chat path uses.
     *
     * `operator` is URL-encoded via the HttpUrl builder (same pattern as
     * [status]). Propagates an [IOException] on any network / non-2xx error out
     * of [BlackBoxApi.get] — the caller (PersonaCache) catches it to fall back to
     * a cached prompt when offline.
     */
    override suspend fun systemPrompt(operator: String): PersonaResponse {
        val url = "${api.getBaseUrl()}/local/system-prompt".toHttpUrl().newBuilder()
            .addQueryParameter("operator", operator)
            .build()
        val path = url.encodedPath + "?" + (url.encodedQuery ?: "")
        val body = api.get(path)
        return json.decodeFromString(PersonaResponse.serializer(), body)
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
