package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalStatus

/**
 * The slice of [LocalModelApi] the Model Manager UI ([LocalModelViewModel],
 * Task 1.5) needs that is NOT already on [LocalModelDownloader]: the downloadable
 * catalog, the per-operator device status (carries autonomy + availability), and
 * the autonomy toggle.
 *
 * Extracted purely for testability — it lets [LocalModelViewModel] be unit-tested
 * against a plain in-memory fake with no MockWebServer / OkHttp. [LocalModelApi]
 * implements it unchanged (signatures are identical), so Task 1.3's tests against
 * the concrete class are untouched.
 */
interface LocalModelCatalogClient {

    /** See [LocalModelApi.catalog]. */
    suspend fun catalog(): List<LocalBundle>

    /** See [LocalModelApi.status]. */
    suspend fun status(operator: String): LocalStatus

    /** See [LocalModelApi.setAutonomy]. */
    suspend fun setAutonomy(operator: String, deviceId: String, mode: String): Boolean
}
