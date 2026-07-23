// Render test for the SPEECH step's voice section (TTS downloads + clone +
// design mini-flows) — Brandon 2026-07-22: SPEECH must cover BOTH directions
// of voice, not just STT. DOM-free, house pattern (audio_section.render.test.mjs):
// onboarding.js touches location/document at import (transcription.js now pulls
// it in transitively via local_models.js), so we shim a minimal DOM first, then
// exercise only the exported PURE helpers against realistic
// GET /local-models/status payloads.
//
// Asserts the task's acceptance shape:
//   1. STT provider cards UNCHANGED (renderCard still emits the radio-card);
//   2. TTS section renders the shared per-variant download rows from status;
//   3. clone/design blocks GATE on their variant being downloaded + healthy;
//   4. consent unchecked → the clone button renders DISABLED;
//   5. stack off/unhealthy → the single-line note only (no buttons); no
//      status at all (older backend 404) → "" (hidden cleanly).
//
// Run: node --test Portal/onboarding/steps/transcription_voice.render.test.mjs
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

const step = await import("./transcription.js");

function countButtons(html) { return (html.match(/<button\b/g) || []).length; }
function countDataDl(html) { return (html.match(/\bdata-dl=/g) || []).length; }

// Status factory: healthy GPU box, the three Qwen variants present.
// downloadedKeys marks which variants are already on disk.
function makeStatus({ installed = true, healthy = true, downloadedKeys = [] } = {}) {
    const dk = new Set(downloadedKeys);
    return {
        installed, enabled: installed, healthy,
        hardware: { gpu: true, gpu_name: "NVIDIA RTX 2000 Ada Generation",
                    vram_mb: 16380, ram_mb: 64000, source: "nvidia-smi", tier: "HIGH" },
        disk: { free_mb: 512000, required_mb: 40960, ok: true },
        models: [
            { model: "qwen-tts", capability: "tts", group: "audio", label: "Qwen3-TTS",
              running: false, state: null, download: { state: "pending" }, downloadable: true,
              artifacts: installed ? [
                  { key: "qwen-tts-base", label: "Qwen3-TTS 1.7B — Base",
                    downloadable: true, downloaded: dk.has("qwen-tts-base"),
                    size_gb: 4.5, repo_pending_g3: false },
                  { key: "qwen-tts-custom-voice", label: "Qwen3-TTS 1.7B — Custom Voice (3s clone)",
                    downloadable: true, downloaded: dk.has("qwen-tts-custom-voice"),
                    size_gb: 4.5, repo_pending_g3: false },
                  { key: "qwen-tts-voice-design", label: "Qwen3-TTS 1.7B — Voice Design (text-described)",
                    downloadable: true, downloaded: dk.has("qwen-tts-voice-design"),
                    size_gb: 4.5, repo_pending_g3: false },
              ] : [] },
        ],
        routing: { tts: { enabled: installed, healthy, decision: healthy ? "on-box" : "off" } },
    };
}

const FRESH = makeStatus();  // healthy, nothing downloaded yet
const READY = makeStatus({ downloadedKeys: ["qwen-tts-base", "qwen-tts-custom-voice", "qwen-tts-voice-design"] });
const OFF = makeStatus({ installed: false, healthy: false });
const UNHEALTHY = makeStatus({ healthy: false });

// ── 1. STT provider cards unchanged ─────────────────────────────────────
test("STT provider cards keep their radio-card shape (untouched by the voice section)", () => {
    const html = step.renderCard(
        { id: "openai", label: "OpenAI", available: true, blurb: "Whisper streaming.",
          models: { streaming: "gpt-realtime-whisper", file: "gpt-4o-transcribe" } },
        { id: "openai", vendor: "OpenAI", needsHint: "Add your OpenAI API key in the Keys step." },
    );
    assert.match(html, /ob-cli-agent-card/);
    assert.match(html, /role="radio"/);
    assert.match(html, /Ready/);
    assert.match(html, /gpt-realtime-whisper/);
    assert.match(html, /gpt-4o-transcribe/);
    // The voice section never leaks into a provider card.
    assert.doesNotMatch(html, /ob-stt-voice/);
});

// ── 2. TTS download rows from a fake status ─────────────────────────────
test("voiceSectionHtml renders the three shared Qwen variant download rows when healthy", () => {
    const html = step.voiceSectionHtml(FRESH);
    assert.match(html, /Text-to-speech \(on-box\)/);
    assert.equal((html.match(/ob-lm-audio-row"/g) || []).length, 3, "three variant rows");
    assert.equal(countDataDl(html), 3, "three wired Download buttons");
    assert.match(html, /data-dl="qwen-tts-base"/);
    assert.match(html, /data-dl="qwen-tts-custom-voice"/);
    assert.match(html, /data-dl="qwen-tts-voice-design"/);
    assert.match(html, /4\.5 GB/);
});

test("a downloaded variant renders the shared done state, not a button", () => {
    const html = step.voiceSectionHtml(READY);
    assert.equal(countDataDl(html), 0, "nothing left to download");
    assert.match(html, /Downloaded/);
});

// ── 3. clone/design gate on downloaded variants + healthy stack ─────────
test("clone/design blocks stay honest notes until their variant is downloaded", () => {
    const html = step.voiceSectionHtml(FRESH);
    assert.match(html, /id="ob-stt-clone" data-ready="false"/);
    assert.match(html, /id="ob-stt-design" data-ready="false"/);
    assert.doesNotMatch(html, /ob-stt-clone-file/, "no upload form before the variant exists");
    assert.doesNotMatch(html, /ob-stt-design-desc/, "no design form before the variant exists");
    assert.match(html, /Custom Voice/);
    assert.match(html, /Voice Design/);

    assert.equal(step.cloneReady(FRESH), false);
    assert.equal(step.designReady(FRESH), false);
    assert.equal(step.cloneReady(READY), true);
    assert.equal(step.designReady(READY), true);
    // Downloaded but stack unhealthy → still not ready (never a dead form).
    const dlButDown = makeStatus({ healthy: false,
        downloadedKeys: ["qwen-tts-custom-voice", "qwen-tts-voice-design"] });
    assert.equal(step.cloneReady(dlButDown), false);
    assert.equal(step.designReady(dlButDown), false);
});

test("downloaded variants + healthy stack unlock the clone and design forms", () => {
    const html = step.voiceSectionHtml(READY);
    assert.match(html, /id="ob-stt-clone" data-ready="true"/);
    assert.match(html, /id="ob-stt-design" data-ready="true"/);
    assert.match(html, /id="ob-stt-clone-file"[^>]*accept="audio\/\*"/);
    assert.match(html, /~3 seconds/, "minimum-length note present");
    assert.match(html, /id="ob-stt-clone-consent"/);
    assert.match(html, /id="ob-stt-design-desc"/);
    assert.match(html, /id="ob-stt-design-text"/, "optional sample-text field");
});

// ── 4. consent unchecked → clone button disabled ────────────────────────
test("the clone button renders DISABLED until consent is checked", () => {
    const html = step.cloneBlockHtml(READY);
    assert.match(html, /<button[^>]*id="ob-stt-clone-btn"[^>]*\bdisabled\b/);
    // The checkbox itself starts unchecked (no checked attribute).
    assert.doesNotMatch(html, /id="ob-stt-clone-consent"[^>]*checked/);
});

// ── 4b. clone success line: at-clone preview (server >= ba81b8fa) ───────
test("clone success with preview_b64 renders an inline audio player with the preview label", () => {
    const html = step.cloneSuccessHtml(
        { voice_id: "my-narrator", preview_b64: "UklGRg==", preview_mime: "audio/wav" },
        "My Narrator",
    );
    assert.match(html, /Voice cloned: <code>my-narrator<\/code>/);
    assert.match(html, /Preview your cloned voice/);
    assert.match(html, /<audio controls[^>]*src="data:audio\/wav;base64,UklGRg=="/);
});

test("clone success from an older backend (no preview fields) is exactly the text-only line", () => {
    const html = step.cloneSuccessHtml({ voice_id: "my-narrator" }, "My Narrator");
    assert.match(html, /Voice cloned: <code>my-narrator<\/code>/);
    assert.doesNotMatch(html, /<audio/);
    assert.doesNotMatch(html, /Preview your cloned voice/);
});

test("clone success falls back to the typed name when voice_id is absent, and escapes it", () => {
    const html = step.cloneSuccessHtml({}, `<b>"Me"</b>`);
    assert.match(html, /&lt;b&gt;/, "name is HTML-escaped");
    assert.doesNotMatch(html, /<b>/);
});

test("a weird preview_mime falls back to audio/wav (no attribute breakout)", () => {
    const html = step.cloneSuccessHtml(
        { voice_id: "v", preview_b64: "QUJD", preview_mime: `audio/wav" onload="x` },
        "v",
    );
    assert.match(html, /src="data:audio\/wav;base64,QUJD"/);
    assert.doesNotMatch(html, /onload/);
});

// ── 5. fail-open states ─────────────────────────────────────────────────
test("stack OFF → the single-line note only: no buttons, no rows, no forms", () => {
    const html = step.voiceSectionHtml(OFF);
    assert.match(html, /local model stack/);
    assert.equal(countButtons(html), 0, "no buttons when the stack is off");
    assert.doesNotMatch(html, /ob-lm-audio-row/);
    assert.doesNotMatch(html, /ob-stt-clone-file/);
});

test("stack installed but UNHEALTHY → note only, no buttons", () => {
    const html = step.voiceSectionHtml(UNHEALTHY);
    assert.match(html, /local model stack/);
    assert.equal(countButtons(html), 0);
});

test("no /local-models/status at all (older backend 404) → section hidden cleanly", () => {
    assert.equal(step.voiceSectionHtml(null), "");
    assert.equal(step.voiceSectionHtml(undefined), "");
});
