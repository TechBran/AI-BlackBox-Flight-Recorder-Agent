// Unit test for the provider-first TWO-STEP TTS voice picker (M-D Task D1,
// user-chosen UX 2026-07-22). Like audio_section.render.test.mjs, tts-stt.js
// touches window/document at import, so we shim a minimal DOM FIRST, then
// exercise the exported pure helpers + the DOM-driving populateVoiceCatalog /
// applyVoiceValue against a fake catalog and fake <select>s.
//
// Asserts the milestone's acceptance shape:
//   1. provider <select> lists the N catalog providers (server order preserved,
//      on-box appended after cloud);
//   2. selecting a provider fills the voice <select> with ONLY that provider's
//      voices;
//   3. the emitted value stays `provider:voice`;
//   4. the default resolves to TTS_DEFAULT_VOICE's provider + voice;
//   5. an unreachable catalog keeps the static fallback (both selects untouched).
//
// Run: node --test Portal/modules/tts-stt.voicepicker.test.mjs
//  (or: node Portal/modules/tts-stt.voicepicker.test.mjs)
import test from "node:test";
import assert from "node:assert/strict";

// ── minimal globals so tts-stt.js (+ its imports) load cleanly ──
globalThis.window = globalThis.window || {};
globalThis.window.addEventListener = globalThis.window.addEventListener || (() => {});
globalThis.Audio = class { constructor() { this.paused = true; } play() {} pause() {} };
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.localStorage = {
    _s: {}, getItem(k) { return this._s[k] ?? null; },
    setItem(k, v) { this._s[k] = String(v); }, removeItem(k) { delete this._s[k]; },
};
const minimalNode = { innerHTML: "", style: {}, appendChild() {}, addEventListener() {},
    querySelector() { return null; }, querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {} } };
globalThis.document = {
    addEventListener() {}, getElementById() { return null; },
    querySelector() { return null; }, createElement() { return { ...minimalNode }; },
    head: minimalNode, body: minimalNode,
};
globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));

const mod = await import("./tts-stt.js");
const {
    providerOptionsFromGroups, voiceOptionsForProvider, providerOfVoiceId,
    resolveVoiceSelection, applyVoiceValue, populateVoiceCatalog,
} = mod;

const TTS_DEFAULT_VOICE = "gemini-pro:Charon";  // mirrors the module constant

// Catalog: cloud groups first (openai, gemini-flash, gemini-pro), on-box (qwen)
// + elevenlabs appended after — the server order the picker must preserve.
const CATALOG = [
    { id: "openai", label: "OpenAI TTS HD", voices: [
        { id: "openai:alloy", name: "Alloy", description: "Neutral, balanced" },
        { id: "openai:nova", name: "Nova", description: "Friendly, upbeat" },
    ] },
    { id: "gemini-flash", label: "Gemini Flash TTS", voices: [
        { id: "gemini-flash:Zephyr", name: "Zephyr", description: "Bright, cheerful" },
    ] },
    { id: "gemini-pro", label: "Gemini Pro TTS", voices: [
        { id: "gemini-pro:Zephyr", name: "Zephyr", description: "Bright, cheerful" },
        { id: "gemini-pro:Charon", name: "Charon", description: "Calm, informative" },
    ] },
    { id: "qwen", label: "Qwen3-TTS (on-box)", voices: [
        { id: "qwen:default", name: "Qwen Default", description: "On-device" },
    ] },
    { id: "elevenlabs", label: "ElevenLabs", voices: [
        { id: "elevenlabs:abc123", name: "Rachel", description: "Cloned" },
    ] },
];

// ── fake DOM ──────────────────────────────────────────────────────────────
class FakeOption { constructor() { this.value = ""; this.textContent = ""; } }
class FakeOptgroup { constructor() { this.label = ""; this._children = []; this.__optgroup = true; }
    appendChild(o) { this._children.push(o); return o; } }
class FakeSelect {
    constructor(id) { this.id = id; this._options = []; this._value = ""; this.onchange = null; this.emits = 0; }
    set innerHTML(v) { if (v === "") this._options = []; }
    get innerHTML() { return ""; }
    appendChild(node) {
        if (node && node.__optgroup) { for (const c of node._children) this._options.push(c); }
        else this._options.push(node);
        return node;
    }
    get options() { return this._options; }
    set value(v) { this._value = v; }
    get value() { return this._value; }
    dispatchEvent() { this.emits++; if (this.onchange) this.onchange(); return true; }
}

function installDom({ withProvider = true, voiceStatic = [] } = {}) {
    const voiceSel = new FakeSelect("ttsVoiceSelect");
    for (const v of voiceStatic) { const o = new FakeOption(); o.value = v; voiceSel._options.push(o); }
    if (voiceStatic.length) voiceSel._value = voiceStatic[voiceStatic.length - 1];
    const providerSel = withProvider ? new FakeSelect("ttsProviderSelect") : null;
    globalThis.document = {
        getElementById(id) {
            if (id === "ttsVoiceSelect") return voiceSel;
            if (id === "ttsProviderSelect") return providerSel;
            return null;
        },
        createElement(tag) { return tag === "optgroup" ? new FakeOptgroup() : new FakeOption(); },
    };
    return { voiceSel, providerSel };
}
function stubCatalog(groups) {
    globalThis.fetch = async () => ({ ok: true, json: async () => ({ groups }) });
}

// ── pure helpers ────────────────────────────────────────────────────────────
test("providerOptionsFromGroups lists every provider in server order", () => {
    const opts = providerOptionsFromGroups(CATALOG);
    assert.deepEqual(opts.map(o => o.value),
        ["openai", "gemini-flash", "gemini-pro", "qwen", "elevenlabs"]);
    assert.match(opts[0].label, /OpenAI TTS HD \(2 voices\)/);
    // on-box (qwen) is appended AFTER the cloud groups, before... server-ordered.
    assert.ok(opts.findIndex(o => o.value === "qwen") > opts.findIndex(o => o.value === "gemini-pro"));
});

test("voiceOptionsForProvider returns ONLY that provider's voices, provider:voice ids", () => {
    const v = voiceOptionsForProvider(CATALOG, "openai");
    assert.deepEqual(v.map(o => o.value), ["openai:alloy", "openai:nova"]);
    assert.ok(v.every(o => o.value.startsWith("openai:")), "every value stays provider:voice");
    assert.equal(voiceOptionsForProvider(CATALOG, "nope").length, 0, "unknown provider → empty");
});

test("providerOfVoiceId resolves by exact match then colon-prefix fallback", () => {
    assert.equal(providerOfVoiceId(CATALOG, "gemini-pro:Charon"), "gemini-pro");
    assert.equal(providerOfVoiceId(CATALOG, "qwen:default"), "qwen");
    // not in catalog → prefix fallback
    assert.equal(providerOfVoiceId(CATALOG, "openai:future-voice"), "openai");
    assert.equal(providerOfVoiceId([], "openai:alloy"), "openai");
});

test("resolveVoiceSelection: wanted value wins, else default, else first voice", () => {
    assert.deepEqual(resolveVoiceSelection(CATALOG, "openai:nova"),
        { providerId: "openai", voiceId: "openai:nova" });
    // wanted absent → falls back to TTS_DEFAULT_VOICE
    assert.deepEqual(resolveVoiceSelection(CATALOG, "bogus:x"),
        { providerId: "gemini-pro", voiceId: TTS_DEFAULT_VOICE });
    // default also absent → first group's first voice
    const noDefault = [{ id: "openai", label: "OpenAI", voices: [{ id: "openai:alloy", name: "Alloy", description: "d" }] }];
    assert.deepEqual(resolveVoiceSelection(noDefault, "bogus:x"),
        { providerId: "openai", voiceId: "openai:alloy" });
    // empty catalog → nulls (caller keeps static fallback)
    assert.deepEqual(resolveVoiceSelection([], "x"), { providerId: null, voiceId: null });
});

// ── DOM-driving populateVoiceCatalog ─────────────────────────────────────────
test("populateVoiceCatalog builds a two-step picker defaulting to TTS_DEFAULT_VOICE", async () => {
    const { voiceSel, providerSel } = installDom();
    stubCatalog(CATALOG);
    await populateVoiceCatalog();

    // (1) provider select lists all N providers
    assert.deepEqual(providerSel.options.map(o => o.value),
        ["openai", "gemini-flash", "gemini-pro", "qwen", "elevenlabs"]);
    // (4) default resolves to TTS_DEFAULT_VOICE provider + voice
    assert.equal(providerSel.value, "gemini-pro");
    assert.equal(voiceSel.value, TTS_DEFAULT_VOICE);
    // (2)+(3) voice select shows ONLY gemini-pro voices, all provider:voice
    assert.deepEqual(voiceSel.options.map(o => o.value), ["gemini-pro:Zephyr", "gemini-pro:Charon"]);
    assert.ok(voiceSel.options.every(o => o.value.startsWith("gemini-pro:")));
});

test("selecting a provider fills the voice select with only that provider's voices + re-emits change", async () => {
    const { voiceSel, providerSel } = installDom();
    stubCatalog(CATALOG);
    await populateVoiceCatalog();

    let emitted = 0;
    voiceSel.onchange = () => { emitted++; };
    providerSel.value = "openai";
    providerSel.onchange();  // simulate the user picking a provider

    assert.deepEqual(voiceSel.options.map(o => o.value), ["openai:alloy", "openai:nova"]);
    assert.equal(voiceSel.value, "openai:alloy", "voice select defaults to the new provider's first voice");
    assert.equal(emitted, 1, "provider switch re-emits change on #ttsVoiceSelect so listeners fire");
});

test("selectId fast-path selects the freshly-added voice + switches provider", async () => {
    const { voiceSel, providerSel } = installDom();
    stubCatalog(CATALOG);
    let emitted = 0;
    voiceSel.onchange = () => { emitted++; };
    await populateVoiceCatalog("elevenlabs:abc123");

    assert.equal(providerSel.value, "elevenlabs");
    assert.equal(voiceSel.value, "elevenlabs:abc123");
    assert.equal(emitted, 1, "selectId fast-path dispatches a change to persist the pick");
});

test("unreachable catalog keeps the static fallback (both selects untouched)", async () => {
    // static voice fallback = the default provider's list (Charon selected)
    const { voiceSel, providerSel } = installDom({
        voiceStatic: ["gemini-pro:Zephyr", "gemini-pro:Charon"],
    });
    const providerOptsBefore = providerSel.options.length;  // provider static set by HTML (0 here)
    globalThis.fetch = async () => ({ ok: false, status: 503, json: async () => ({}) });
    await populateVoiceCatalog();

    // fetch failed → both selects left exactly as the static HTML had them
    assert.equal(voiceSel.options.length, 2, "voice static options untouched");
    assert.equal(voiceSel.value, "gemini-pro:Charon", "static default preserved");
    assert.equal(providerSel.options.length, providerOptsBefore, "provider select untouched");
});

test("empty catalog keeps the static fallback", async () => {
    const { voiceSel } = installDom({ voiceStatic: ["gemini-pro:Charon"] });
    stubCatalog([]);  // groups present but empty
    await populateVoiceCatalog();
    assert.equal(voiceSel.options.length, 1, "empty catalog → static options kept");
    assert.equal(voiceSel.value, "gemini-pro:Charon");
});

// ── applyVoiceValue (syncVoiceDropdown restore path) ─────────────────────────
test("applyVoiceValue switches provider + refills voices to expose a cross-provider saved voice", async () => {
    const { voiceSel, providerSel } = installDom();
    stubCatalog(CATALOG);
    await populateVoiceCatalog();  // primes the cached groups; shows gemini-pro
    assert.equal(providerSel.value, "gemini-pro");

    applyVoiceValue("openai:nova");  // operator's saved voice from a different provider
    assert.equal(providerSel.value, "openai", "provider select switched to the saved voice's provider");
    assert.deepEqual(voiceSel.options.map(o => o.value), ["openai:alloy", "openai:nova"]);
    assert.equal(voiceSel.value, "openai:nova", "voice select now exposes the saved voice");
});

// ── legacy single-select surface (no provider select) still grouped ──────────
test("degrades to grouped single-select when #ttsProviderSelect is absent", async () => {
    const { voiceSel } = installDom({ withProvider: false });
    stubCatalog(CATALOG);
    await populateVoiceCatalog();
    // all voices flattened into one select (optgroups collapsed by the fake)
    const vals = voiceSel.options.map(o => o.value);
    assert.ok(vals.includes("openai:alloy") && vals.includes("gemini-pro:Charon") && vals.includes("qwen:default"),
        "legacy path keeps every group's voices in the single select");
    assert.equal(voiceSel.value, TTS_DEFAULT_VOICE);
});
