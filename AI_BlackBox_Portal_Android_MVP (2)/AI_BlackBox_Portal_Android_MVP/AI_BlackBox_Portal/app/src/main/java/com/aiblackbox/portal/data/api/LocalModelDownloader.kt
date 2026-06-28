package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import java.io.File

/**
 * The slice of [LocalModelApi] that the orchestration layer
 * ([com.aiblackbox.portal.data.local.LocalModelManager]) actually calls:
 * stream a bundle to disk, and attest a verified model with the hub.
 *
 * Extracted purely for testability — it lets LocalModelManager be unit-tested
 * against a plain in-memory fake with no MockWebServer / OkHttp / Android
 * Context. [LocalModelApi] implements it unchanged; callers can keep using the
 * concrete class directly (Task 1.3 tests are untouched). Intentionally
 * minimal: catalog()/status() are not part of the manager's contract.
 */
interface LocalModelDownloader {

    /** Stream [bundle] to [destFile] (resumable, direct from HF). See LocalModelApi.download. */
    suspend fun download(
        bundle: LocalBundle,
        destFile: File,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<File>

    /** Record a verified on-device model with the hub. See LocalModelApi.attest. */
    suspend fun attest(req: AttestRequest): Boolean
}
