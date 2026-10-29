package com.aiblackbox.portal.data.repository

import com.aiblackbox.portal.data.api.ApiHttpException
import com.aiblackbox.portal.data.model.ModelCatalog
import kotlinx.coroutines.test.runTest
import org.junit.Assert.assertEquals
import org.junit.Assert.assertFalse
import org.junit.Assert.assertNotNull
import org.junit.Assert.assertNull
import org.junit.Assert.assertTrue
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TemporaryFolder
import java.io.IOException

/**
 * The shared model catalog (audit A01-activities-root-10, rank 76): one copy for the chat
 * composer, Settings, cron, SMS and CU, a short TTL, a last-good copy on disk, and custom
 * never cached. Driven with a fake GET /models/{provider} and a fake clock; the disk store
 * is the real file store on a temp folder.
 */
class ModelCatalogRepositoryTest {

    @get:Rule val tmp = TemporaryFolder()

    /** A fake GET /models/{provider}: answers [bodies] (an Exception = that call fails). */
    private class FakeBox(var bodies: MutableMap<String, Any> = mutableMapOf()) {
        val calls = mutableListOf<String>()
        val fetch: suspend (String) -> String = { provider ->
            calls += provider
            when (val b = bodies[provider]) {
                is Exception -> throw b
                is String -> b
                else -> throw ApiHttpException("HTTP 404: Not Found", 404)
            }
        }
    }

    private var clock = 1_000_000L

    private fun repo(box: FakeBox, store: LastGoodCatalogStore? = null) =
        ModelCatalogRepository(box.fetch, store, now = { clock })

    private val anthropic = """{"provider":"anthropic","source":"live","default_id":"claude-opus-5-5",
        "models":[{"id":"claude-opus-5-5","name":"Claude Opus 5.5"},{"id":"claude-sonnet-5-5","name":"Claude Sonnet 5.5"}]}"""

    // ── parsing + picker rows ────────────────────────────────────────────────

    @Test fun `a catalog lists its models with Auto first`() {
        val c = ModelCatalog.parse("anthropic", anthropic, 0, fromBox = true)!!
        assertEquals(listOf("claude-opus-5-5", "claude-sonnet-5-5"), c.ids)
        assertEquals(
            listOf("" to "Auto - Latest", "claude-opus-5-5" to "Claude Opus 5.5", "claude-sonnet-5-5" to "Claude Sonnet 5.5"),
            c.options(),
        )
        assertTrue(c.isAuthoritative)
        assertEquals(emptyMap<String, String>(), c.backends())
        assertEquals(emptyMap<String, String>(), c.statuses())
    }

    @Test fun `odd entries are skipped, a body with no model list is not a catalog`() {
        val c = ModelCatalog.parse(
            "openai",
            """{"models":[{"id":"gpt-5.5"},{"name":"no id"},"a string",{"id":""},{"id":"o4-mini","name":null}]}""",
            0, fromBox = true,
        )!!
        assertEquals(listOf("gpt-5.5", "o4-mini"), c.ids)
        // A missing or null name shows the id.
        assertEquals(listOf("" to "Auto - Latest", "gpt-5.5" to "gpt-5.5", "o4-mini" to "o4-mini"), c.options())
        assertNull(ModelCatalog.parse("openai", """{"error":"nope"}""", 0, true))
        assertNull(ModelCatalog.parse("openai", "<html>502</html>", 0, true))
        assertNull(ModelCatalog.parse("openai", "[]", 0, true))
        // An empty list is a catalog: the box lists nothing.
        assertEquals(emptyList<Pair<String, String>>(), ModelCatalog.parse("openai", """{"models":[]}""", 0, true)!!.options())
    }

    @Test fun `computer-use rows name the default and carry each model's backend`() {
        val c = ModelCatalog.parse(
            "computer-use",
            """{"source":"live","default_id":"gemini-cu","models":[
               {"id":"claude-opus-5-5","name":"Claude Opus 5.5","backend":"anthropic"},
               {"id":"gemini-cu","name":"Gemini CU","backend":"google"}]}""",
            0, fromBox = true,
        )!!
        assertEquals("" to "Auto - Gemini CU", c.options().first())
        assertEquals(
            mapOf("claude-opus-5-5" to "anthropic", "gemini-cu" to "google", "" to "google"),
            c.backends(),
        )
        // Default unknown: Auto lands with anthropic, the box's own default backend.
        val noDefault = ModelCatalog.parse(
            "computer-use", """{"models":[{"id":"gpt-cu","backend":"openai"}]}""", 0, true,
        )!!
        assertEquals("" to "Auto - Latest", noDefault.options().first())
        assertEquals("anthropic", noDefault.backends()[""])
    }

    @Test fun `custom rows carry load status, absent or null status is left out`() {
        val c = ModelCatalog.parse(
            "custom",
            """{"source":"live","default_id":"box::a","models":[
               {"id":"box::a","name":"a (box)","status":"loaded","server":"box"},
               {"id":"box::b","name":"b (box)","status":null},{"id":"box::c"}]}""",
            0, fromBox = true,
        )!!
        assertEquals("" to "Auto - a (box)", c.options().first())
        assertEquals(mapOf("box::a" to "loaded"), c.statuses())
    }

    @Test fun `only a fresh live non-empty catalog may replace stored ids`() {
        assertFalse(ModelCatalog.parse("openai", """{"source":"fallback","models":[{"id":"x"}]}""", 0, true)!!.isAuthoritative)
        assertFalse(ModelCatalog.parse("openai", """{"source":"live","models":[]}""", 0, true)!!.isAuthoritative)
        assertFalse(ModelCatalog.parse("openai", """{"source":"live","models":[{"id":"x"}]}""", 0, false)!!.isAuthoritative)
        // computer-use merges three vendors; one that did not answer live is backfilled with
        // the box's static entries, so the merged list cannot say a pick is gone.
        val cu = """{"source":"live","models":[{"id":"claude-opus-5-5","backend":"anthropic"}],"backends":"""
        assertTrue(ModelCatalog.parse("computer-use", cu + """{"anthropic":"live","google":"live","openai":"live"}}""", 0, true)!!.isAuthoritative)
        assertFalse(ModelCatalog.parse("computer-use", cu + """{"anthropic":"error","google":"live","openai":"live"}}""", 0, true)!!.isAuthoritative)
        assertFalse(ModelCatalog.parse("computer-use", cu + """{"anthropic":"live","google":"fallback","openai":"live"}}""", 0, true)!!.isAuthoritative)
        // An odd vendor map never costs the catalog itself.
        assertNotNull(ModelCatalog.parse("computer-use", cu + """{"anthropic":{"x":1}}}""", 0, true))
    }

    // ── TTL, failure, custom ─────────────────────────────────────────────────

    @Test fun `within the TTL the memory copy answers, after it the box is asked again`() = runTest {
        val box = FakeBox(mutableMapOf("anthropic" to anthropic))
        val r = repo(box)
        assertNull("nothing known before the first answer", r.cached("anthropic"))

        assertEquals(2, r.refresh("anthropic")!!.models.size)
        clock += ModelCatalogRepository.TTL_MS - 1
        r.refresh("anthropic")
        assertEquals(listOf("anthropic"), box.calls)

        clock += 1
        r.refresh("anthropic")
        assertEquals(listOf("anthropic", "anthropic"), box.calls)
        // force skips the TTL.
        r.refresh("anthropic", force = true)
        assertEquals(3, box.calls.size)
    }

    @Test fun `a failed fetch answers null and keeps the last good copy shown`() = runTest {
        val box = FakeBox(mutableMapOf("anthropic" to anthropic))
        val r = repo(box)
        r.refresh("anthropic")
        clock += ModelCatalogRepository.TTL_MS
        box.bodies["anthropic"] = IOException("unreachable")

        assertNull(r.refresh("anthropic"))
        assertEquals(listOf("claude-opus-5-5", "claude-sonnet-5-5"), r.cached("anthropic")!!.ids)
        // The restore check never trusts a stale copy: unknown, so the switch stays on Auto.
        assertNull(r.liveIds("anthropic"))

        box.bodies["anthropic"] = """{"error":"nope"}"""
        assertNull("a body that is not a catalog is not an answer", r.refresh("anthropic"))
        assertEquals(2, r.cached("anthropic")!!.ids.size)
    }

    @Test fun `an empty catalog is an answer but is not kept`() = runTest {
        val box = FakeBox(mutableMapOf("anthropic" to anthropic))
        val r = repo(box)
        r.refresh("anthropic")
        clock += ModelCatalogRepository.TTL_MS
        box.bodies["anthropic"] = """{"models":[]}"""
        assertEquals(emptyList<String>(), r.liveIds("anthropic"))
        assertEquals("the last good copy stays", 2, r.cached("anthropic")!!.ids.size)
    }

    @Test fun `custom is never cached at any layer`() = runTest {
        val store = FileLastGoodCatalogStore(tmp.newFolder(), "http://box:9091")
        val box = FakeBox(mutableMapOf("custom" to """{"source":"live","models":[{"id":"box::a"}]}"""))
        val r = repo(box, store)
        r.refresh("custom")
        r.refresh("custom")
        assertEquals("every look asks the box", listOf("custom", "custom"), box.calls)
        assertNull(r.cached("custom"))
        assertNull(store.read("custom"))
        // An emptied registry is real state: the answer is the empty list.
        box.bodies["custom"] = """{"source":"live","models":[]}"""
        assertEquals(emptyList<String>(), r.liveIds("custom"))
    }

    // ── the last-good copy on disk ───────────────────────────────────────────

    @Test fun `a cold start lists the last catalog the box served, but never trusts it`() = runTest {
        val dir = tmp.newFolder()
        repo(FakeBox(mutableMapOf("anthropic" to anthropic)), FileLastGoodCatalogStore(dir, "http://box:9091"))
            .refresh("anthropic")

        // A new process: nothing in memory, the box unreachable.
        val down = FakeBox(mutableMapOf("anthropic" to IOException("offline")))
        val restarted = repo(down, FileLastGoodCatalogStore(dir, "http://box:9091"))
        val c = restarted.cached("anthropic")!!
        assertEquals(listOf("claude-opus-5-5", "claude-sonnet-5-5"), c.ids)
        assertFalse(c.fromBox)
        assertFalse("a disk copy never replaces stored ids", c.isAuthoritative)
        assertEquals(clock, c.fetchedAtMs)
        assertTrue(down.calls.isEmpty())

        // Another box's catalogs are its own.
        assertNull(repo(down, FileLastGoodCatalogStore(dir, "http://other:9091")).cached("anthropic"))
    }

    @Test fun `a damaged last-good file is set aside and read as nothing`() {
        val dir = tmp.newFolder()
        val store = FileLastGoodCatalogStore(dir, "http://box:9091")
        store.write("openai", """{"models":[{"id":"gpt-5.5"}]}""", 5)
        val file = dir.walkTopDown().first { it.name == "openai.json" }
        file.writeText("{not json")

        assertNull(store.read("openai"))
        assertFalse(file.exists())
        assertTrue(file.parentFile!!.listFiles()!!.any { it.name.startsWith("openai.json.corrupt-") })
        // A provider name that is not a plain key is never a path.
        store.write("../escape", "{}", 0)
        assertNull(store.read("../escape"))
        assertFalse(dir.resolve("escape.json").exists())
    }

    @Test fun `every catalog provider maps to its box key, the others have none`() {
        assertEquals("google", ModelCatalogRepository.apiProviderFor("gemini"))
        for (p in listOf("anthropic", "openai", "xai", "computer-use", "custom")) {
            assertEquals(p, ModelCatalogRepository.apiProviderFor(p))
        }
        for (p in listOf("local", "agents", "gemini-agents", "realtime", "gemini-live", "grok-live", "robotics")) {
            assertNull(p, ModelCatalogRepository.apiProviderFor(p))
        }
    }

    @Test fun `the shared registry hands every screen the same copy per box`() {
        ModelCatalogs.clear()
        val a = com.aiblackbox.portal.data.api.BlackBoxApi("http://box:9091")
        val b = com.aiblackbox.portal.data.api.BlackBoxApi("http://box:9091")
        val other = com.aiblackbox.portal.data.api.BlackBoxApi("http://other:9091")
        assertTrue(ModelCatalogs.forApi(a) === ModelCatalogs.forApi(b))
        assertFalse(ModelCatalogs.forApi(a) === ModelCatalogs.forApi(other))
        assertNotNull(ModelCatalogs.forApi(other))
        ModelCatalogs.clear()
    }
}
