package com.aiblackbox.portal.data.api

import com.aiblackbox.portal.data.model.PersonaResponse

/**
 * The single-method slice of [LocalModelApi] that fetches the on-device
 * persona / system prompt (GET /local/system-prompt).
 *
 * Extracted purely for testability — it lets [com.aiblackbox.portal.data.local.PersonaCache]
 * be unit-tested against a plain in-memory fake with no MockWebServer / OkHttp.
 * [LocalModelApi] implements it unchanged (the signature is identical), so the
 * MockWebServer tests against the concrete class are untouched. Mirrors the
 * [LocalModelCatalogClient] / [LocalModelDownloader] seam pattern.
 */
interface PersonaSource {

    /** See [LocalModelApi.systemPrompt]. */
    suspend fun systemPrompt(operator: String): PersonaResponse
}
