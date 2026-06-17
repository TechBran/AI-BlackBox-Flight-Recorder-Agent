package com.aiblackbox.portal.data.local

import com.aiblackbox.portal.data.api.PersonaSource
import com.aiblackbox.portal.data.model.PersonaResponse
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertTrue
import org.junit.Test
import java.io.IOException

/**
 * Unit tests for [PersonaCache] — fetch-once + cache-with-offline-fallback for
 * the on-device persona / system prompt (GET /local/system-prompt).
 *
 * Pure JVM, no Android: the API is faked behind the tiny [PersonaSource] seam
 * and the store behind the in-memory [FakePersonaStore], so SharedPreferences /
 * Context never enter the testable core. Coverage:
 *   1. online   → returns the fetched prompt AND saves {prompt, version}.
 *   2. offline + cached   → returns the cached prompt (no throw).
 *   3. offline + no cache → returns the documented fallback (no throw).
 *   4. online refresh → a new version overwrites the stored value (not stale).
 */
class PersonaCacheTest {

    /** In-memory [PersonaStore] keyed by operator; records save() calls. */
    private class FakePersonaStore : PersonaStore {
        val saved = LinkedHashMap<String, PersonaResponse>()
        var saveCount = 0
        override fun load(operator: String): PersonaResponse? = saved[operator]
        override fun save(operator: String, persona: PersonaResponse) {
            saveCount++
            saved[operator] = persona
        }
    }

    /** Fake [PersonaSource]: returns [next] or throws [error] if set. */
    private class FakePersonaSource(
        var next: PersonaResponse? = null,
        var error: Throwable? = null,
    ) : PersonaSource {
        var calls = 0
        override suspend fun systemPrompt(operator: String): PersonaResponse {
            calls++
            error?.let { throw it }
            return next ?: error("FakePersonaSource: no response scripted")
        }
    }

    @Test
    fun `get online returns fetched prompt and saves it to the store`() = runTest {
        val api = FakePersonaSource(next = PersonaResponse(prompt = "PERSONA-V1", version = "v1"))
        val store = FakePersonaStore()
        val cache = PersonaCache(api, store)

        val prompt = cache.get("Brandon")

        assertEquals("returns the fetched prompt", "PERSONA-V1", prompt)
        assertEquals("fetched exactly once", 1, api.calls)
        assertEquals("saved once", 1, store.saveCount)
        val saved = store.load("Brandon")
        assertNotNull("must be cached", saved)
        assertEquals("cached prompt matches", "PERSONA-V1", saved!!.prompt)
        assertEquals("cached version matches", "v1", saved.version)
    }

    @Test
    fun `get offline with a cached value returns the cached prompt`() = runTest {
        val store = FakePersonaStore()
        store.save("Brandon", PersonaResponse(prompt = "CACHED-PERSONA", version = "v7"))
        // API is offline: any call throws.
        val api = FakePersonaSource(error = IOException("no network"))
        val cache = PersonaCache(api, store)

        val prompt = cache.get("Brandon")

        assertEquals("falls back to the cached prompt", "CACHED-PERSONA", prompt)
        assertEquals("tried the network once", 1, api.calls)
    }

    @Test
    fun `get offline with no cache returns the documented fallback`() = runTest {
        val api = FakePersonaSource(error = IOException("no network"))
        val store = FakePersonaStore() // empty
        val cache = PersonaCache(api, store)

        val prompt = cache.get("Stranger")

        assertEquals(
            "never-fetched-and-offline yields the documented fallback",
            PersonaCache.FALLBACK_PERSONA,
            prompt,
        )
        // Sanity: the fallback is a non-empty, sensible one-liner.
        assertTrue("fallback is non-blank", prompt.isNotBlank())
    }

    @Test
    fun `get online refresh overwrites a stale cached version`() = runTest {
        val store = FakePersonaStore()
        store.save("Brandon", PersonaResponse(prompt = "OLD-PERSONA", version = "v1"))
        val api = FakePersonaSource(next = PersonaResponse(prompt = "NEW-PERSONA", version = "v2"))
        val cache = PersonaCache(api, store)

        val prompt = cache.get("Brandon")

        assertEquals("returns the freshly fetched prompt", "NEW-PERSONA", prompt)
        val saved = store.load("Brandon")!!
        assertEquals("cache refreshed to new prompt", "NEW-PERSONA", saved.prompt)
        assertEquals("cache refreshed to new version", "v2", saved.version)
    }
}
