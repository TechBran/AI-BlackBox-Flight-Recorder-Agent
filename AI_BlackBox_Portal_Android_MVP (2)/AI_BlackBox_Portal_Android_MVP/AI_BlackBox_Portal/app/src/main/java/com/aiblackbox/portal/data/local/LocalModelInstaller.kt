package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.model.LocalBundle

/**
 * The slice of [LocalModelManager] the Model Manager UI ([LocalModelViewModel],
 * Task 1.5) calls: list on-disk models, recommend a bundle by device RAM, run
 * the full install flow, and delete a bundle.
 *
 * Extracted purely for testability — it lets [LocalModelViewModel] be unit-tested
 * against a plain in-memory fake with no Android Context / disk / network.
 * [LocalModelManager] implements it unchanged (the method signatures are
 * identical), so Task 1.4's tests against the concrete class are untouched.
 */
interface LocalModelInstaller {

    /** See [LocalModelManager.installedModels]. */
    suspend fun installedModels(): List<InstalledModel>

    /** See [LocalModelManager.recommendForDevice]. */
    suspend fun recommendForDevice(bundles: List<LocalBundle>): LocalBundle

    /** See [LocalModelManager.install]. */
    suspend fun install(
        bundle: LocalBundle,
        operator: String,
        delegate: String,
        onProgress: (bytesSoFar: Long, totalBytes: Long) -> Unit,
    ): Result<InstalledModel>

    /** See [LocalModelManager.delete]. */
    suspend fun delete(slug: String): Boolean
}
