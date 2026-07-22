// Behavior test for the local_models wizard step's decision/formatting logic
// against a REALISTIC GET /local-models/status payload (Task 8.6 review item 5).
//
// There is no browser JS test infra on this box, and jsdom is not installed, so
// this is a DOM-free Node test: the step module imports onboarding.js which
// touches `location`/`document` at module top-level, so we install a minimal
// shim BEFORE importing, then exercise only the exported pure helpers (no
// render()/innerHTML — those need a real DOM). That is enough to reproduce the
// exact bug this task fixes: on the real payload, routing[cap] is an OBJECT
// {enabled,healthy,decision}, and the old isActive() called .toLowerCase() on
// it → TypeError → the whole step stuck on "Checking your hardware…". A green
// run here proves the step reads the real shape (hardware/disk/routing/models).
//
// Run: node --test Portal/onboarding/steps/local_models.render.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

// ── minimal DOM shim so onboarding.js's top-level code imports cleanly ──
// onboarding.js runs a top-level async IIFE that fetches state and, on failure,
// writes into getElementById("ob-step-container"). We make fetch reject (no
// network in a unit test) and hand back a permissive fake node so that IIFE
// settles quietly instead of leaking an unhandled rejection into the runner.
const fakeNode = {
    innerHTML: "", style: {},
    addEventListener() {}, querySelector() { return null; },
    querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {} },
};
globalThis.location = { search: "" };
globalThis.document = {
    addEventListener() {}, getElementById() { return fakeNode; },
    querySelector() { return null; }, createElement() { return { ...fakeNode }; },
    head: fakeNode, body: fakeNode,
};
globalThis.window = globalThis.window || {};
globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));

const step = await import("./local_models.js");

// A realistic GPU-tier payload, matching Orchestrator/routes/local_models_routes.py
// (hardware = verbatim hardware.probe(); disk = free_mb/required_mb; routing[cap]
// = {enabled,healthy,decision}; models[] keyed by `model` with a download entry).
const GPU_STATUS = {
    installed: true, enabled: true, healthy: true,
    base_url: "http://127.0.0.1:9098/v1",
    hardware: { gpu: true, gpu_name: "NVIDIA RTX 2000 Ada Generation",
                vram_mb: 16380, ram_mb: 64000, source: "nvidia-smi", tier: "HIGH" },
    disk: { free_mb: 512000, required_mb: 40960, ok: true },
    models: [
        { model: "embed-qwen3-8b", capability: "embeddings", group: "retrieval",
          label: "Qwen3-Embedding-8B (Q8_0)", running: false, state: null,
          download: { state: "pending" }, downloadable: true },
        { model: "rerank-qwen3-8b", capability: "rerank", group: "retrieval",
          label: "Qwen3-Reranker-8B (Q8_0)", running: false, state: null,
          download: { state: "done" }, downloadable: false },
        { model: "speaches", capability: "stt", group: "audio",
          label: "Speaches", running: true, state: "ready",
          download: { state: "downloaded" }, downloadable: false },
        { model: "qwen-tts", capability: "tts", group: "audio",
          label: "Qwen3-TTS", running: false, state: null,
          download: { state: "pending" }, downloadable: true },
    ],
    routing: {
        embeddings: { enabled: true, healthy: true, decision: "on-box" },
        rerank: { enabled: false, healthy: true, decision: "off" },
        stt: { enabled: true, healthy: true, decision: "on-box" },
        tts: { enabled: true, healthy: false, decision: "unhealthy" },
    },
};

const CPU_EMPTY_STATUS = {
    installed: false, enabled: false, healthy: false,
    hardware: { gpu: false, gpu_name: null, vram_mb: null, ram_mb: 8000,
                source: "none", tier: "LOW" },
    disk: { free_mb: null, required_mb: 40960, ok: null },
    models: [],
    routing: {
        embeddings: { enabled: false, healthy: false, decision: "off" },
        rerank: { enabled: false, healthy: false, decision: "off" },
        stt: { enabled: false, healthy: false, decision: "off" },
        tts: { enabled: false, healthy: false, decision: "off" },
    },
};

// Fresh-box GPU payload: NOTHING downloaded yet. Every member's download
// defaults to {state:"pending"} (local_models_routes.py). embeddings + tts are
// fetchable (downloadable:true); rerank (self-converted) + stt (whisper auto-
// pulled) are NOT — this is the exact state that used to render a Download
// button whose POST 404s "Unknown artifact".
const FRESH_STATUS = {
    installed: true, enabled: true, healthy: true,
    base_url: "http://127.0.0.1:9098/v1",
    hardware: { gpu: true, gpu_name: "NVIDIA RTX 2000 Ada Generation",
                vram_mb: 16380, ram_mb: 64000, source: "nvidia-smi", tier: "HIGH" },
    disk: { free_mb: 512000, required_mb: 40960, ok: true },
    models: [
        { model: "embed-qwen3-8b", capability: "embeddings", group: "retrieval",
          label: "Qwen3-Embedding-8B (Q8_0)", running: false, state: null,
          download: { state: "pending" }, downloadable: true },
        { model: "rerank-qwen3-8b", capability: "rerank", group: "retrieval",
          label: "Qwen3-Reranker-8B (Q8_0)", running: false, state: null,
          download: { state: "pending" }, downloadable: false },
        { model: "speaches", capability: "stt", group: "audio",
          label: "Speaches", running: false, state: null,
          download: { state: "pending" }, downloadable: false },
        { model: "qwen-tts", capability: "tts", group: "audio",
          label: "Qwen3-TTS", running: false, state: null,
          download: { state: "pending" }, downloadable: true },
    ],
    routing: {
        embeddings: { enabled: false, healthy: true, decision: "off" },
        rerank: { enabled: false, healthy: true, decision: "off" },
        stt: { enabled: false, healthy: true, decision: "off" },
        tts: { enabled: false, healthy: true, decision: "off" },
    },
};

test("isActive reads routing[cap].decision without throwing (the fixed TypeError)", () => {
    // Old code: (routing.stt || "").toLowerCase() → TypeError on the object.
    assert.doesNotThrow(() => step.isActive("stt", GPU_STATUS));
    assert.equal(step.isActive("embeddings", GPU_STATUS), true);
    assert.equal(step.isActive("stt", GPU_STATUS), true);
    assert.equal(step.isActive("rerank", GPU_STATUS), false);   // decision "off"
    assert.equal(step.isActive("tts", GPU_STATUS), false);      // "unhealthy" ≠ on-box
    // matches the backend sentinel, hyphenated — never 'onbox'
    assert.equal(step.isActive("embeddings", { routing: { embeddings: { decision: "onbox" } } }), false);
});

test("tierKey reads status.hardware.tier (GPU no longer misdetected as CPU)", () => {
    assert.equal(step.tierKey(GPU_STATUS), "gpu");
    assert.equal(step.tierKey(CPU_EMPTY_STATUS), "cpu");
    // a GPU with only tier (no top-level status.gpu, which never existed)
    assert.equal(step.tierKey({ hardware: { gpu: true, tier: "HIGH" } }), "gpu");
});

test("hwLineHtml renders the GPU name + VRAM from status.hardware", () => {
    const line = step.hwLineHtml(GPU_STATUS);
    assert.match(line, /RTX 2000/);
    assert.match(line, /16 GB VRAM/);              // 16380 MB → 16 GB
    assert.match(step.hwLineHtml(CPU_EMPTY_STATUS), /No GPU detected/);
});

test("diskLineHtml renders from free_mb/required_mb (not free_gb)", () => {
    const line = step.diskLineHtml(GPU_STATUS);
    assert.match(line, /Disk free: <strong>500 GB<\/strong>/); // 512000 MB → 500 GB
    assert.match(line, /needs ~40 GB/);                         // 40960 MB → 40 GB
    assert.equal(step.diskLineHtml(CPU_EMPTY_STATUS), "");      // free_mb null → dropped
});

test("modelForCap keys off models[].capability and returns the artifact id", () => {
    assert.equal(step.modelForCap("embeddings", GPU_STATUS).model, "embed-qwen3-8b");
    assert.equal(step.modelForCap("tts", GPU_STATUS).model, "qwen-tts");
    assert.equal(step.modelForCap("embeddings", CPU_EMPTY_STATUS), null);
});

test("isDownloaded reflects the download-state contract (done/downloaded)", () => {
    assert.equal(step.isDownloaded(step.modelForCap("rerank", GPU_STATUS)), true);   // done
    assert.equal(step.isDownloaded(step.modelForCap("stt", GPU_STATUS)), true);      // downloaded
    assert.equal(step.isDownloaded(step.modelForCap("embeddings", GPU_STATUS)), false); // pending
    assert.equal(step.isDownloaded(null), true);   // no member → nothing to download
});

test("renderCapRow emits a valid Download button (data-dl=model, never undefined)", () => {
    const html = step.renderCapRow({ id: "embeddings", label: "Memory" }, GPU_STATUS);
    assert.match(html, /data-dl="embed-qwen3-8b"/);
    assert.doesNotMatch(html, /undefined/);
});

test("renderCapRow shows activate/deactivate for downloaded weights", () => {
    // stt: downloaded + on-box active → 'turn off' (data-off)
    const stt = step.renderCapRow({ id: "stt", label: "Speech" }, GPU_STATUS);
    assert.match(stt, /data-off="stt"/);
    assert.doesNotMatch(stt, /data-dl=/);   // NOT a Download button — already present
    // rerank: downloaded but not active → 'Use on-box' (data-on)
    const rk = step.renderCapRow({ id: "rerank", label: "Reranking" }, GPU_STATUS);
    assert.match(rk, /data-on="rerank"/);
});

test("renderCapRow never shows a Download button for a non-manifest member (fresh-box pending)", () => {
    // The regression: on a fresh box rerank/stt default to download.state
    // 'pending', so isDownloaded() is false — but their ids aren't in
    // DOWNLOAD_MANIFEST (downloadable:false), so a Download button would POST an
    // unknown artifact and 404. They must fall through to 'Use on-box' + an
    // auto-provision note instead.
    const rk = step.renderCapRow({ id: "rerank", label: "Reranking" }, FRESH_STATUS);
    assert.doesNotMatch(rk, /data-dl=/);          // never a Download button
    assert.match(rk, /data-on="rerank"/);          // activation only
    assert.match(rk, /provisioned automatically/); // the honest note replaces it

    const stt = step.renderCapRow({ id: "stt", label: "Speech" }, FRESH_STATUS);
    assert.doesNotMatch(stt, /data-dl=/);
    assert.match(stt, /data-on="stt"/);
    assert.match(stt, /provisioned automatically/);

    // ...while the genuinely fetchable members in the SAME payload still offer
    // their (correct, manifest-backed) Download button.
    const emb = step.renderCapRow({ id: "embeddings", label: "Memory" }, FRESH_STATUS);
    assert.match(emb, /data-dl="embed-qwen3-8b"/);
    const tts = step.renderCapRow({ id: "tts", label: "Voice" }, FRESH_STATUS);
    assert.match(tts, /data-dl="qwen-tts"/);
});

test("isDownloadable gates on the backend downloadable flag", () => {
    assert.equal(step.isDownloadable(step.modelForCap("embeddings", FRESH_STATUS)), true);
    assert.equal(step.isDownloadable(step.modelForCap("rerank", FRESH_STATUS)), false);
    assert.equal(step.isDownloadable(step.modelForCap("stt", FRESH_STATUS)), false);
    assert.equal(step.isDownloadable(null), false);
});

test("renderCapRow tolerates the empty/CPU payload (no member, no throw)", () => {
    assert.doesNotThrow(() => {
        const html = step.renderCapRow({ id: "tts", label: "Voice" }, CPU_EMPTY_STATUS);
        assert.doesNotMatch(html, /undefined/);
    });
});
