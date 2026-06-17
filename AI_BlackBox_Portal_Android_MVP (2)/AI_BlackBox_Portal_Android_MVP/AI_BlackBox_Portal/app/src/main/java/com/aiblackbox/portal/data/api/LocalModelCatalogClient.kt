package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.AttestRequest
import com.aiblackbox.portal.data.model.LocalBundle
import com.aiblackbox.portal.data.model.LocalStatus

/**
 * The slice of [LocalModelApi] the Model Manager UI ([LocalModelViewModel],
 * Task 1.5) needs that is NOT already on [LocalModelDownloader]: the downloadable
 * catalog, the per-operator device status (carries autonomy + availability), the
 * autonomy toggle, and a device attestation (so the autonomy toggle can self-heal
 * an unattested device — see [LocalModelViewModel.setAutonomy]).
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

    /**
     * Register (upsert) this device's verified on-device model with the hub —
     * see [LocalModelApi.attest]. Idempotent (re-attest is safe). Used by the
     * autonomy toggle's best-effort self-heal: if the backend mirror 404s
     * because this device was never attested in THAT hub's registry (e.g. a
     * SIDELOADED model that bypassed the install→attest flow), the toggle
     * attests once and retries the mirror. Returns whether attestation
     * succeeded (a failure is swallowed — the local posture already holds).
     */
    suspend fun attest(req: AttestRequest): Boolean
}
