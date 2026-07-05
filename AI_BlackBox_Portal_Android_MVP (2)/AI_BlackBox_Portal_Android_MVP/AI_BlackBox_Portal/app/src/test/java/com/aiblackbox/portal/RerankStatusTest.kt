package com.aiblackbox.portal

import com.aiblackbox.portal.data.model.RerankAction
import com.aiblackbox.portal.data.model.RerankModel
import com.aiblackbox.portal.data.model.RerankSelectRequest
import com.aiblackbox.portal.data.model.RerankStatus
import com.aiblackbox.portal.data.model.rerankActionFor
import com.aiblackbox.portal.data.model.tierModels
import kotlinx.serialization.json.Json
import org.junit.Assert.*
import org.junit.Test

/**
 * M12 — the Android reranker card contract. Mirrors the ChatViewModelSaveTest
 * philosophy (this repo unit-tests pure logic + serialization rather than
 * instantiating an AndroidViewModel, which needs Robolectric/Application the
 * offline unit gate lacks): we prove (1) RerankStatus deserializes the M10
 * `model_catalog` (NOT the flat `models`) tolerating unknown keys, (2) the
 * tier + key gating mirrors the M10.1/M11 selector, and (3) the POST shapes
 * (keyed = no api_key; paste = with api_key). The ViewModel's refreshRerank/
 * selectRerank are literal forwards over these pieces + the repository.
 */
class RerankStatusTest {

    // Same Json config the UpdateRepository uses (ignoreUnknownKeys + lenient,
    // encodeDefaults OFF so a null api_key is omitted from the select body).
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    /** A realistic LOW-box payload: the live flat `models` list + base_url +
     *  candidate_n are all UNKNOWN to our subset and must be dropped, while the
     *  M10 `model_catalog` array deserializes into typed RerankModel entries. */
    private val liveLowPayload = """
        {
          "enabled": true,
          "gpu": false,
          "service_reachable": false,
          "provider": "cohere",
          "model": "cohere-rerank-4",
          "model_id": "rerank-v4.0-pro",
          "base_url": "http://localhost:8091",
          "configured": true,
          "preflight": {"latency_ms": 161.3, "measured_ms": 161.3, "ceiling_ms": 1200.0,
                        "passage_n": 1, "state": "ok", "reason": null},
          "available": true,
          "candidate_n": 40,
          "passage_chars": 4096,
          "models": ["cohere-rerank-4", "voyage-rerank-2.5", "qwen3-reranker-0.6b"],
          "tier": "LOW",
          "ram_mb": 31167,
          "reachable": true,
          "auth_kind": "bearer_env",
          "key_present": true,
          "preflight_ceiling_ms": 1200.0,
          "model_catalog": [
            {"slug":"cohere-rerank-4","provider":"cohere","label":"Cohere Rerank 4",
             "tiers":["LOW","MID","HIGH"],"privacy":"cloud","auth_kind":"bearer_env",
             "key_env":"COHERE_API_KEY","key_present":true,"cost_note":"~$2/1K","quality_note":"Dedicated cross-encoder"},
            {"slug":"voyage-rerank-2.5","provider":"voyage","label":"Voyage Rerank 2.5",
             "tiers":["LOW","MID","HIGH"],"privacy":"cloud","auth_kind":"bearer_env",
             "key_env":"VOYAGE_API_KEY","key_present":false,"cost_note":"free tier","quality_note":"Recommended cloud default"},
            {"slug":"llm-rerank-gemini-flash","provider":"llm","label":"Gemini Flash (LLM reranker)",
             "tiers":["LOW","MID","HIGH"],"privacy":"cloud","auth_kind":"frontier_key",
             "key_env":"GOOGLE_API_KEY","key_present":false,"cost_note":"~cents/query","quality_note":"Not a purpose-trained ranker"},
            {"slug":"vertex-semantic-ranker","provider":"vertex","label":"Vertex Semantic Ranker",
             "tiers":["LOW","MID","HIGH"],"privacy":"cloud","auth_kind":"gcp_service_account",
             "key_env":null,"key_present":false,"cost_note":"~$1/1K","quality_note":"Advanced: GCP project + SA"},
            {"slug":"qwen3-reranker-0.6b","provider":"vllm","label":"Qwen3 Reranker 0.6B",
             "tiers":["HIGH"],"privacy":"local","auth_kind":"none",
             "key_env":null,"key_present":false,"cost_note":"Local GPU — no API cost","quality_note":"Default post-GPU pick"},
            {"slug":"qwen3-reranker-0.6b-cpu","provider":"cpu","label":"Qwen3 Reranker 0.6B (CPU)",
             "tiers":["MID"],"privacy":"local","auth_kind":"none",
             "key_env":null,"key_present":false,"cost_note":"Local CPU","quality_note":"MID-tier opt-in"}
          ]
        }
    """.trimIndent()

    // ── (1) serialization: model_catalog, not the flat `models` ────────────

    @Test fun `deserializes model_catalog with ignoreUnknownKeys`() {
        val s = json.decodeFromString(RerankStatus.serializer(), liveLowPayload)
        assertEquals("LOW", s.tier)
        assertTrue(s.enabled)
        assertEquals("cohere", s.provider)
        assertEquals("cohere-rerank-4", s.model)
        assertFalse(s.gpu)
        // The typed catalog parsed (unknown top-level `models`/base_url dropped).
        assertEquals(6, s.modelCatalog.size)
        val voyage = s.modelCatalog.first { it.slug == "voyage-rerank-2.5" }
        assertEquals("voyage", voyage.provider)
        assertEquals("VOYAGE_API_KEY", voyage.keyEnv)
        assertFalse(voyage.keyPresent)
        assertEquals(listOf("LOW", "MID", "HIGH"), voyage.tiers)
        assertEquals(161.3, s.preflight?.latencyMs)
        assertEquals("ok", s.preflight?.state)
    }

    @Test fun `unknown-field-only payload still decodes (defaults hold)`() {
        val s = json.decodeFromString(RerankStatus.serializer(), """{"something_new":42}""")
        assertFalse(s.enabled)
        assertNull(s.tier)
        assertTrue(s.modelCatalog.isEmpty())
    }

    @Test fun `round-trips a RerankStatus`() {
        val original = json.decodeFromString(RerankStatus.serializer(), liveLowPayload)
        val encoded = json.encodeToString(RerankStatus.serializer(), original)
        val decoded = json.decodeFromString(RerankStatus.serializer(), encoded)
        assertEquals(original, decoded)
    }

    // ── (2) tier gating ────────────────────────────────────────────────────

    @Test fun `tierModels keeps only models whose tiers include the box tier`() {
        val s = json.decodeFromString(RerankStatus.serializer(), liveLowPayload)
        val slugs = s.tierModels().map { it.slug }.toSet()
        // LOW box: cloud + LLM present; HIGH-only vLLM and MID-only CPU excluded.
        assertTrue(slugs.contains("cohere-rerank-4"))
        assertTrue(slugs.contains("voyage-rerank-2.5"))
        assertTrue(slugs.contains("llm-rerank-gemini-flash"))
        assertTrue(slugs.contains("vertex-semantic-ranker"))
        assertFalse(slugs.contains("qwen3-reranker-0.6b"))       // HIGH only
        assertFalse(slugs.contains("qwen3-reranker-0.6b-cpu"))   // MID only
    }

    // ── (2) per-model action gating (mirrors Portal optionHtml) ────────────

    private fun model(
        slug: String, provider: String, keyPresent: Boolean = false,
        tiers: List<String> = listOf("LOW", "MID", "HIGH"),
    ) = RerankModel(slug = slug, provider = provider, keyPresent = keyPresent, tiers = tiers)

    private fun status(
        model: String? = null, enabled: Boolean = false,
        gpu: Boolean = false, serviceReachable: Boolean = false, tier: String = "LOW",
    ) = RerankStatus(model = model, enabled = enabled, gpu = gpu,
        serviceReachable = serviceReachable, tier = tier)

    @Test fun `cloud key gate — keyed voyage selectable, un-keyed offers paste`() {
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(model("voyage-rerank-2.5", "voyage", keyPresent = true), status()))
        assertEquals(RerankAction.NEEDS_KEY_PASTE,
            rerankActionFor(model("voyage-rerank-2.5", "voyage", keyPresent = false), status()))
        assertEquals(RerankAction.NEEDS_KEY_PASTE,
            rerankActionFor(model("cohere-rerank-4", "cohere", keyPresent = false), status()))
    }

    @Test fun `llm gate — keyed selectable, un-keyed deep-links (no paste)`() {
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(model("llm-rerank-gpt-mini", "llm", keyPresent = true), status()))
        assertEquals(RerankAction.NEEDS_KEY_LINK,
            rerankActionFor(model("llm-rerank-gpt-mini", "llm", keyPresent = false), status()))
    }

    @Test fun `vertex is selectable once the SA is uploaded, else Advanced`() {
        // Brandon live-test 2026-07-05: an uploaded Google SA makes Vertex
        // key_present=true (backend resolves it from GOOGLE_APPLICATION_CREDENTIALS)
        // → SELECTABLE; without it, deep-link the SA setup (VERTEX_ADVANCED).
        assertEquals(RerankAction.VERTEX_ADVANCED,
            rerankActionFor(model("vertex-semantic-ranker", "vertex", keyPresent = false), status()))
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(model("vertex-semantic-ranker", "vertex", keyPresent = true), status()))
    }

    @Test fun `the active selection renders ACTIVE`() {
        val m = model("cohere-rerank-4", "cohere", keyPresent = true)
        assertEquals(RerankAction.ACTIVE,
            rerankActionFor(m, status(model = "cohere-rerank-4", enabled = true)))
        // Same model but reranking disabled → selectable, not active.
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(m, status(model = "cohere-rerank-4", enabled = false)))
    }

    @Test fun `cpu is in-process selectable, local vllm gated on gpu plus service`() {
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(model("qwen3-reranker-0.6b-cpu", "cpu"), status()))
        assertEquals(RerankAction.LOCAL_UNAVAILABLE,
            rerankActionFor(model("qwen3-reranker-0.6b", "vllm"),
                status(gpu = false, serviceReachable = false)))
        assertEquals(RerankAction.SELECTABLE,
            rerankActionFor(model("qwen3-reranker-0.6b", "vllm"),
                status(gpu = true, serviceReachable = true)))
    }

    // ── (3) POST shapes — keyed omits api_key, paste includes it ───────────

    @Test fun `select body for a keyed provider omits api_key`() {
        val body = json.encodeToString(
            RerankSelectRequest.serializer(),
            RerankSelectRequest(provider = "cohere", model = "cohere-rerank-4", enabled = true),
        )
        assertFalse("keyed select must not carry api_key: $body", body.contains("api_key"))
        assertTrue(body.contains("\"provider\":\"cohere\""))
        assertTrue(body.contains("\"model\":\"cohere-rerank-4\""))
        assertTrue(body.contains("\"enabled\":true"))
    }

    @Test fun `select body for a paste includes api_key`() {
        val body = json.encodeToString(
            RerankSelectRequest.serializer(),
            RerankSelectRequest(provider = "voyage", model = "voyage-rerank-2.5",
                enabled = true, apiKey = "FAKE-TOKEN-FOR-TEST"),
        )
        assertTrue("paste select must carry api_key: $body", body.contains("\"api_key\":\"FAKE-TOKEN-FOR-TEST\""))
    }

    @Test fun `turn-off select body carries enabled false`() {
        val body = json.encodeToString(
            RerankSelectRequest.serializer(),
            RerankSelectRequest(provider = "cohere", model = "cohere-rerank-4", enabled = false),
        )
        assertTrue(body.contains("\"enabled\":false"))
        assertFalse(body.contains("api_key"))
    }
}
