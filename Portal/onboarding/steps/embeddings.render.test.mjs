// Behavior test for the two-card Memory step redesign
// (Portal/onboarding/steps/embeddings.js). The redesign splits the step into
// CARD 1 (embedding model, grouped Cloud / On-the-box, with a per-model
// Download button and an Advanced disclosure) and CARD 2 (the reranker, lifted
// out to its OWN top-level card, its on-box branch keyed on the backend `built`
// flag). There's no browser JS test infra + no jsdom on this box, so — unlike
// the sibling local_models test — embeddings.js has ZERO top-level DOM/import
// dependencies (no import of onboarding.js; render() is the only DOM toucher
// and isn't called here), so it imports cleanly in bare Node and we exercise
// the exported pure HTML/decision helpers directly.
//
// Run: node --test Portal/onboarding/steps/embeddings.render.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

const step = await import("./embeddings.js");

// An ACTIVE on-box embedder: weights ARE present (active-implies-downloaded),
// so the backend emits NO "not downloaded" blocker.
const ACTIVE_ONBOX = {
    slug: "qwen3-embedding-8b-local",
    label: "Qwen3 Embedding 8B (on-box CUDA · Q8_0 · max quality)",
    privacy: "local", dims: 4096, ram_gb: 8, cost_per_1m_tokens: 0,
    member_id: "embed-qwen3-8b", downloadable: true,
    blockers: [], store_exists: true, missing: 0, ready: true,
};

// The SAME on-box embedder before its GGUF is fetched: the backend emits the
// "not downloaded" blocker (only ever when downloadable && !downloaded && !active).
const ONBOX_ABSENT = {
    slug: "qwen3-embedding-8b-local",
    label: "Qwen3 Embedding 8B (on-box CUDA · Q8_0 · max quality)",
    privacy: "local", dims: 4096, ram_gb: 8, cost_per_1m_tokens: 0,
    member_id: "embed-qwen3-8b", downloadable: true,
    blockers: ["On-box weights not downloaded yet — use the Download button to fetch them (~8 GB)."],
    store_exists: false, missing: null, ready: false,
};

test("an ACTIVE on-box model shows NO download blocker", () => {
    assert.equal(step.embNeedsDownload(ACTIVE_ONBOX), false);
    assert.equal(step.embDownloadBtnHtml(ACTIVE_ONBOX, {}), "");
});

test("an on-box embedder with the GGUF absent shows a Download button", () => {
    assert.equal(step.embNeedsDownload(ONBOX_ABSENT), true);
    const html = step.embDownloadBtnHtml(ONBOX_ABSENT, {});
    assert.match(html, /Download/);
    assert.match(html, /ob-emb-dl/);                    // the wired button class
    assert.match(html, /data-member="embed-qwen3-8b"/); // POSTs {artifact: member_id}
});

test("cloud models never carry a download button", () => {
    const cloud = { slug: "gemini-embedding-001", label: "Gemini Embedding 001",
        privacy: "cloud", dims: 1536, ram_gb: 0, cost_per_1m_tokens: 0.15,
        member_id: null, downloadable: false, blockers: [], store_exists: true, missing: 0, ready: true };
    assert.equal(step.embNeedsDownload(cloud), false);
    assert.equal(step.embDownloadBtnHtml(cloud, {}), "");
});

test("the quantization badge is read LIVE from the label (never hardcoded)", () => {
    assert.match(step.quantBadge("Qwen3 Embedding 8B (on-box CUDA · Q8_0 · max quality)"), /Q8_0/);
    assert.match(step.quantBadge("Qwen3 Embedding 8B (Ollama · Q4)"), /Q4/);
    assert.equal(step.quantBadge("Gemini Embedding 001"), ""); // no quant token → no chip
});

// ── CARD 2 — the reranker, its OWN top-level card ──
const RR_BASE = {
    tier: "HIGH", tier_guidance: "This box has a GPU — the local reranker is free.",
    preflight: {},
    model_catalog: [
        { slug: "voyage-rerank-2.5", provider: "voyage", label: "Voyage rerank-2.5 (cloud)",
          tiers: ["LOW", "MID", "HIGH"], privacy: "cloud", built: false, key_present: true },
        { slug: "qwen3-reranker-8b-local", provider: "localstack",
          label: "Qwen3 Reranker 8B Q8_0 (on-box, llama-swap)",
          tiers: ["MID", "HIGH"], privacy: "local", built: false, key_present: false },
    ],
};

test("the reranker renders as a SEPARATE top-level card", () => {
    const html = step.rerankCardHtml({ ...RR_BASE, enabled: false, model: null, provider: "null" });
    assert.match(html, /ob-emb-card/);        // a top-level card wrapper, not buried in Compute
    assert.match(html, /id="ob-emb-rerank"/);
    assert.match(html, /Reranker/);
    // two sub-headers, not per-card badges
    assert.match(html, /Cloud \/ your keys/);
    assert.match(html, /On the box/);
    // cloud voyage with a present key → selectable
    assert.match(html, /Use this reranker/);
});

test("a NOT-built on-box reranker shows an honest note, never a fake build endpoint", () => {
    const html = step.rerankCardHtml({ ...RR_BASE, enabled: false, model: null, provider: "null" });
    // No build endpoint exists yet → an honest "provisioned by setup" note, no button.
    assert.match(html, /provisioned by setup|coming to the\s+wizard/);
    assert.doesNotMatch(html, /local-models\/reranker\/build/);
});

test("a BUILT + active on-box reranker shows the green Validated state", () => {
    const rr = {
        ...RR_BASE, enabled: true, model: "qwen3-reranker-8b-local", provider: "localstack",
        model_catalog: RR_BASE.model_catalog.map((m) =>
            m.slug === "qwen3-reranker-8b-local" ? { ...m, built: true } : m),
    };
    const html = step.rerankCardHtml(rr);
    assert.match(html, /Validated/);
    assert.match(html, /Turn reranking off/); // the turn-off affordance when rerank is ON
});

test("rerankCardHtml hides entirely on an older backend (null status)", () => {
    assert.equal(step.rerankCardHtml(null), "");
});
