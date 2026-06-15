package com.aiblackbox.portal.data.local

import android.content.Context
import com.aiblackbox.portal.data.api.PersonaSource
import com.aiblackbox.portal.data.model.PersonaResponse
import java.io.IOException

/**
 * A tiny key-value abstraction (keyed by operator) over the persisted persona,
 * isolating the testable [PersonaCache] core from Android's SharedPreferences.
 *
 * The production implementation ([SharedPrefsPersonaStore]) lives in this file
 * and is the ONLY place SharedPreferences is touched; unit tests substitute a
 * plain in-memory fake.
 */
interface PersonaStore {
    /** The cached persona for [operator], or null if none was ever saved. */
    fun load(operator: String): PersonaResponse?

    /** Persist [persona] (prompt + version) for [operator], replacing any prior. */
    fun save(operator: String, persona: PersonaResponse)
}

/**
 * Fetches the on-device persona / system prompt once and caches it so on-device
 * reasoning has the SAME persona / tone / anti-sycophancy text as the cloud chat
 * path — and KEEPS WORKING OFFLINE after that first successful fetch.
 *
 * [get] is fetch-through-and-cache:
 *  - **Online:** fetch GET /local/system-prompt (via [PersonaSource]), save the
 *    {prompt, version} to [store] (refreshing any stale cache), return the prompt.
 *  - **Offline (network/HTTP error) WITH a cache:** return the cached prompt.
 *  - **Offline WITH no cache:** return [FALLBACK_PERSONA].
 *
 * The Android framework (Context / SharedPreferences) is confined to
 * [fromContext] + [SharedPrefsPersonaStore]; this core depends only on the
 * [PersonaSource] and [PersonaStore] seams, so it is plain-JUnit testable.
 *
 * Consumed by sendViaLocalEngine (Task 2.4), which passes the returned String to
 * [FcLoop] (Task 2.2) as its persona param.
 */
class PersonaCache(
    private val api: PersonaSource,
    private val store: PersonaStore,
) {

    /**
     * The persona for [operator]: freshly fetched + cached when online, the
     * cached copy when offline, or [FALLBACK_PERSONA] when offline and never
     * fetched. Never throws for a network failure.
     */
    suspend fun get(operator: String): String {
        return try {
            val fetched = api.systemPrompt(operator)
            store.save(operator, fetched)
            fetched.prompt
        } catch (e: IOException) {
            // Offline / unreachable hub / non-2xx (BlackBoxApi.get throws
            // IOException for all of these). Serve the last-known persona if we
            // ever fetched one; otherwise the documented fallback.
            store.load(operator)?.prompt ?: FALLBACK_PERSONA
        }
    }

    companion object {
        /**
         * The persona used ONLY when the prompt has never been fetched AND the
         * hub is unreachable (first run, fully offline). A deliberately minimal
         * one-liner — the real, full persona replaces it on the first successful
         * fetch and is cached for all later offline use.
         */
        const val FALLBACK_PERSONA: String =
            "You are the AI BlackBox on-device assistant. Be direct, accurate, and " +
                "helpful; do not be sycophantic. If unsure, say so."

        private const val PREFS_NAME = "bbx_persona_cache"
        private const val KEY_PROMPT_PREFIX = "prompt:"
        private const val KEY_VERSION_PREFIX = "version:"

        /**
         * Build a [PersonaCache] wired to a SharedPreferences-backed store. This
         * is the only entry point that touches Android; the resulting cache's
         * core stays framework-free.
         */
        fun fromContext(context: Context, api: PersonaSource): PersonaCache =
            PersonaCache(api, SharedPrefsPersonaStore(context.applicationContext))
    }

    /**
     * Production [PersonaStore] persisting {prompt, version} per operator in a
     * dedicated SharedPreferences file. The ONLY Android-touching code path.
     */
    private class SharedPrefsPersonaStore(context: Context) : PersonaStore {
        private val prefs =
            context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)

        override fun load(operator: String): PersonaResponse? {
            val prompt = prefs.getString(KEY_PROMPT_PREFIX + operator, null) ?: return null
            val version = prefs.getString(KEY_VERSION_PREFIX + operator, "") ?: ""
            return PersonaResponse(prompt = prompt, version = version)
        }

        override fun save(operator: String, persona: PersonaResponse) {
            prefs.edit()
                .putString(KEY_PROMPT_PREFIX + operator, persona.prompt)
                .putString(KEY_VERSION_PREFIX + operator, persona.version)
                .apply()
        }
    }
}
