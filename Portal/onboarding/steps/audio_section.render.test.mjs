// Render test for the local_models wizard step's audio two-card section
// (M-C Task C1). DOM-free, like local_models.render.test.mjs: onboarding.js
// touches location/document at import, so we shim a minimal DOM first, then
// exercise only the exported PURE helpers (audioArtifacts / audioSectionHtml /
// audioArtifactBtnHtml) against a realistic GET /local-models/status payload
// carrying the A4 per-artifact rows (status.models[].artifacts).
//
// Asserts the milestone's acceptance shape:
//   1. two cards (STT + TTS) when the stack is on;
//   2. exactly four download buttons (1 whisper + 3 Qwen variants);
//   3. a repo_pending_g3 artifact → a DISABLED button ("pinned during first
//      GPU bring-up"), never a live (data-dl) button that would 404;
//   4. INERT WHEN OFF — no artifacts → the section renders "" (no cards).
//
// Run: node --test Portal/onboarding/steps/audio_section.render.test.mjs
//  (or: node Portal/onboarding/steps/audio_section.render.test.mjs)
import test from "node:test";
import assert from "node:assert/strict";

// ── minimal DOM shim so onboarding.js's top-level code imports cleanly ──
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

// Count <button> elements in an HTML string.
function countButtons(html) {
    return (html.match(/<button\b/g) || []).length;
}
function countDisabledButtons(html) {
    return (html.match(/<button\b[^>]*\bdisabled\b/g) || []).length;
}
function countDataDl(html) {
    return (html.match(/\bdata-dl=/g) || []).length;
}

// GPU-tier, stack ON. The audio MEMBERS (speaches=stt, qwen-tts=tts) carry the
// A4 `artifacts` rows the backend hangs off them. whisper is repo_pending_g3
// (disabled button); the three Qwen variants are live download buttons.
const AUDIO_STATUS = {
    installed: true, enabled: true, healthy: true,
    hardware: { gpu: true, gpu_name: "NVIDIA RTX 2000 Ada Generation",
                vram_mb: 16380, ram_mb: 64000, source: "nvidia-smi", tier: "HIGH" },
    disk: { free_mb: 512000, required_mb: 40960, ok: true },
    recommendations: {
        stt: { label: "whisper large-v3-turbo (stream) + large-v3 (files)", size_gb: 3 },
    },
    models: [
        { model: "speaches", capability: "stt", group: "audio", label: "Speaches",
          running: false, state: null, download: { state: "pending" }, downloadable: false,
          artifacts: [
              { key: "whisper", label: "Whisper (faster-whisper large-v3 turbo + batch)",
                downloadable: false, downloaded: false, size_gb: 3.0, repo_pending_g3: true },
          ] },
        { model: "qwen-tts", capability: "tts", group: "audio", label: "Qwen3-TTS",
          running: false, state: null, download: { state: "pending" }, downloadable: true,
          artifacts: [
              { key: "qwen-tts-base", label: "Qwen3-TTS 1.7B — Base",
                downloadable: true, downloaded: false, size_gb: 4.5, repo_pending_g3: false },
              { key: "qwen-tts-custom-voice", label: "Qwen3-TTS 1.7B — Custom Voice (3s clone)",
                downloadable: true, downloaded: false, size_gb: 4.5, repo_pending_g3: false },
              { key: "qwen-tts-voice-design", label: "Qwen3-TTS 1.7B — Voice Design (text-described)",
                downloadable: true, downloaded: false, size_gb: 4.5, repo_pending_g3: false },
          ] },
    ],
    routing: {
        stt: { enabled: true, healthy: true, decision: "on-box" },
        tts: { enabled: true, healthy: true, decision: "on-box" },
    },
};

// Stack OFF (dev box, no [local_models]): is_installed() False → the backend
// emits NO `artifacts` on the audio members (or the members are absent).
const OFF_STATUS = {
    installed: false, enabled: false, healthy: false,
    hardware: { gpu: false, gpu_name: null, vram_mb: null, ram_mb: 8000, source: "none", tier: "LOW" },
    disk: { free_mb: null, required_mb: 40960, ok: null },
    models: [],
    routing: {
        stt: { enabled: false, healthy: false, decision: "off" },
        tts: { enabled: false, healthy: false, decision: "off" },
    },
};

test("audioSectionHtml renders TWO cards (STT + TTS) when the stack is on", () => {
    const html = step.audioSectionHtml(AUDIO_STATUS);
    assert.match(html, /id="ob-lm-audio-stt"/);
    assert.match(html, /id="ob-lm-audio-tts"/);
    assert.equal((html.match(/ob-lm-audio-card"/g) || []).length, 2, "exactly two cards");
    // STT card names the auto-selected best-fit model (no manual dropdown).
    assert.match(html, /Best fit for your GPU/);
    assert.match(html, /whisper large-v3-turbo/);
    assert.doesNotMatch(html, /<select/, "no manual model dropdown in phase 1");
});

test("audioSectionHtml renders exactly FOUR download buttons (1 whisper + 3 Qwen)", () => {
    const html = step.audioSectionHtml(AUDIO_STATUS);
    assert.equal(countButtons(html), 4, "one whisper + three Qwen variant buttons");
    // The three live Qwen variants are wired (data-dl); whisper is pending → not.
    assert.equal(countDataDl(html), 3, "only the live variants carry data-dl");
});

test("a repo_pending_g3 artifact renders a DISABLED button, never a live 404 button", () => {
    const pending = { key: "whisper", label: "Whisper", downloadable: false,
                      downloaded: false, size_gb: 3.0, repo_pending_g3: true };
    const html = step.audioArtifactBtnHtml(pending, {});
    assert.match(html, /disabled/);
    assert.match(html, /Pinned during first GPU bring-up/i);
    assert.doesNotMatch(html, /data-dl=/, "pending button must NOT be wired to a download POST");

    // And in the assembled section, the single disabled button is the whisper one.
    const section = step.audioSectionHtml(AUDIO_STATUS);
    assert.equal(countDisabledButtons(section), 1, "exactly the whisper button is disabled");
});

test("a live (not pending, not downloaded) artifact renders a wired Download button", () => {
    const live = { key: "qwen-tts-base", label: "Qwen3-TTS 1.7B — Base",
                   downloadable: true, downloaded: false, size_gb: 4.5, repo_pending_g3: false };
    const html = step.audioArtifactBtnHtml(live, {});
    assert.match(html, /data-dl="qwen-tts-base"/);
    assert.match(html, /Download/);
    assert.match(html, /4\.5 GB/);
    assert.doesNotMatch(html, /disabled/);
});

test("a downloaded artifact renders a done state, not a button", () => {
    const done = { key: "qwen-tts-base", label: "Base", downloadable: true,
                   downloaded: true, size_gb: 4.5, repo_pending_g3: false };
    const html = step.audioArtifactBtnHtml(done, {});
    assert.equal(countButtons(html), 0);
    assert.match(html, /Downloaded/);
});

test("a downloading artifact renders a live progress bar", () => {
    const a = { key: "qwen-tts-base", label: "Base", downloadable: true,
                downloaded: false, size_gb: 4.5, repo_pending_g3: false };
    const html = step.audioArtifactBtnHtml(a, { "qwen-tts-base": { completed: 5, total: 10, statusText: "downloading" } });
    assert.match(html, /ob-lm-progress-fill/);
    assert.match(html, /50%/);
});

test("INERT WHEN OFF — no artifacts → no section, no cards", () => {
    assert.equal(step.audioArtifacts("stt", OFF_STATUS).length, 0);
    assert.equal(step.audioArtifacts("tts", OFF_STATUS).length, 0);
    assert.equal(step.audioSectionHtml(OFF_STATUS), "", "stack off → empty section");
    // Older backend: audio members present but WITHOUT an artifacts array.
    const legacy = { models: [
        { model: "speaches", capability: "stt", group: "audio", label: "Speaches" },
        { model: "qwen-tts", capability: "tts", group: "audio", label: "Qwen3-TTS" },
    ] };
    assert.equal(step.audioSectionHtml(legacy), "", "no artifacts array → empty section");
});

test("audioArtifacts reads the member backing each capability", () => {
    assert.equal(step.audioArtifacts("stt", AUDIO_STATUS).length, 1);
    assert.equal(step.audioArtifacts("tts", AUDIO_STATUS).length, 3);
    assert.equal(step.audioArtifacts("stt", AUDIO_STATUS)[0].key, "whisper");
});
