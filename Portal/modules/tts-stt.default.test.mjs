// The Portal takes its TTS default from GET /tts/catalog (audit
// P02-voice-web-08/-09, rank 39).
//
// Before: TTS_MODEL "tts-1" and TTS_FMT "mp3" (window.PORTAL, which nothing
// ever sets) overrode config.ini [audio] on every OpenAI request, and every
// operator without a saved voice spoke "gemini-pro:Charon" -- on a box with no
// Google key too. Now the default voice is the catalog's `default`, the model
// and format are left to the server, and with no catalog at all the request
// names nothing and the server applies its own default.
//
// Run: node --test Portal/modules/tts-stt.default.test.mjs
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

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
    querySelector() { return null; }, querySelectorAll() { return []; },
    createElement() { return { ...minimalNode }; },
    head: minimalNode, body: minimalNode,
};
globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));

const mod = await import("./tts-stt.js");
const {
    getCurrentVoice, parseVoiceValue, catalogDefaultVoice, ensureTtsCatalog,
    resolveVoiceSelection, providerOptionsFromGroups, generateTTSAudioWithVoice,
} = mod;

const GROUPS = [
    { id: "openai", label: "OpenAI TTS HD", available: false, model: "tts-1-hd",
      unavailable_reason: "Needs OPENAI_API_KEY",
      voices: [{ id: "openai:alloy", name: "Alloy", description: "d" },
               { id: "openai:onyx", name: "Onyx", description: "d" }] },
    { id: "gemini-flash", label: "Gemini Flash TTS", available: true,
      model: "gemini-2.5-flash-tts",
      voices: [{ id: "gemini-flash:Kore", name: "Kore", description: "d" }] },
    { id: "elevenlabs", label: "ElevenLabs", available: true, model: "eleven_v3",
      voices: [{ id: "elevenlabs:own1", name: "Mine", description: "cloned" }] },
];

function stubFetch(handler) {
    const calls = [];
    globalThis.fetch = async (url, opts = {}) => {
        calls.push({ url, body: opts.body ? JSON.parse(opts.body) : null });
        return handler(url, opts);
    };
    return calls;
}

// ── no catalog yet: nothing is pinned ───────────────────────────────────────
test("before any catalog, an operator with no saved voice names no voice", () => {
    localStorage.setItem("bbx_operator", "tester");
    assert.equal(getCurrentVoice(), "");
    assert.deepEqual(parseVoiceValue(""), { provider: "", voice: "" });
    assert.deepEqual(parseVoiceValue(getCurrentVoice()), { provider: "", voice: "" });
});

test("parseVoiceValue splits on the first colon; bare names stay OpenAI", () => {
    assert.deepEqual(parseVoiceValue("elevenlabs:abc"), { provider: "elevenlabs", voice: "abc" });
    assert.deepEqual(parseVoiceValue("gemini-pro:Charon"), { provider: "gemini-pro", voice: "Charon" });
    assert.deepEqual(parseVoiceValue("onyx"), { provider: "openai", voice: "onyx" });
});

test("the module carries no pinned TTS model, format or voice", () => {
    // Code only: doc comments may name example ids.
    const src = readFileSync(new URL("./tts-stt.js", import.meta.url), "utf8")
        .replace(/\/\*[\s\S]*?\*\//g, "")
        .replace(/(^|[^:])\/\/.*$/gm, "$1");
    assert.ok(!/["']tts-1(-hd)?["']/.test(src), "no OpenAI TTS model literal");
    assert.ok(!src.includes("gemini-pro:Charon"), "no default voice literal");
    assert.ok(!/gemini-2\.5-(pro|flash)-tts/.test(src), "no Gemini TTS model literal");
    assert.ok(!src.includes("window.PORTAL"), "no dead window.PORTAL lookup");
});

// ── catalogDefaultVoice ─────────────────────────────────────────────────────
test("catalogDefaultVoice: the server default when it is a listed voice", () => {
    assert.equal(catalogDefaultVoice(GROUPS, { id: "elevenlabs:own1" }), "elevenlabs:own1");
});

test("catalogDefaultVoice: else the first AVAILABLE group's first voice", () => {
    assert.equal(catalogDefaultVoice(GROUPS, null), "gemini-flash:Kore");
    assert.equal(catalogDefaultVoice(GROUPS, { id: "gone:voice" }), "gemini-flash:Kore");
    const none = GROUPS.map(g => ({ ...g, available: false }));
    assert.equal(catalogDefaultVoice(none, null), "openai:alloy", "all unavailable -> first voice");
    assert.equal(catalogDefaultVoice([], null), null);
});

test("resolveVoiceSelection falls back to the given default, then the first available voice", () => {
    assert.deepEqual(resolveVoiceSelection(GROUPS, "bogus:x", "elevenlabs:own1"),
        { providerId: "elevenlabs", voiceId: "elevenlabs:own1" });
    assert.deepEqual(resolveVoiceSelection(GROUPS, "bogus:x", null),
        { providerId: "gemini-flash", voiceId: "gemini-flash:Kore" });
});

test("providerOptionsFromGroups marks groups the box has no key for", () => {
    const opts = providerOptionsFromGroups(GROUPS);
    assert.match(opts[0].label, /OpenAI TTS HD \(2 voices\)/);
    assert.match(opts[0].label, /not configured/);
    assert.doesNotMatch(opts[1].label, /not configured/);
});

// ── with the catalog loaded ─────────────────────────────────────────────────
test("ensureTtsCatalog loads the server default; getCurrentVoice uses it", async () => {
    const calls = stubFetch(async () => ({
        ok: true,
        json: async () => ({ groups: GROUPS,
                             default: { id: "elevenlabs:own1", provider: "elevenlabs" } }),
    }));
    await ensureTtsCatalog();
    assert.deepEqual(calls.map(c => c.url), ["/tts/catalog"]);
    assert.equal(getCurrentVoice(), "elevenlabs:own1");
    await ensureTtsCatalog();
    assert.equal(calls.length, 1, "fetched once, then cached");
    // A saved per-operator voice still wins over the box default.
    localStorage.setItem("bb_operator_voices", JSON.stringify({ tester: "gemini-flash:Kore" }));
    assert.equal(getCurrentVoice(), "gemini-flash:Kore");
    localStorage.removeItem("bb_operator_voices");
});

test("the voice preview speaks every provider through /tts with no model or format", async () => {
    globalThis.URL.createObjectURL = () => "blob:preview";
    const calls = stubFetch(async () => ({ ok: true, blob: async () => new Blob(["x"]) }));
    const url = await generateTTSAudioWithVoice("Hi", { provider: "gemini-flash", voice: "Kore" });
    assert.equal(url, "blob:preview");
    assert.equal(calls[0].url, "/tts");
    assert.deepEqual(calls[0].body, { text: "Hi", provider: "gemini-flash", voice: "gemini-flash:Kore" });

    await generateTTSAudioWithVoice("Hi", { provider: "openai", voice: "nova" });
    assert.deepEqual(calls[1].body, { text: "Hi", provider: "openai", voice: "openai:nova" });

    await generateTTSAudioWithVoice("Hi", { provider: "", voice: "" });
    assert.deepEqual(calls[2].body, { text: "Hi" }, "nothing chosen: the server default applies");
});

test("a fallback envelope from /tts is a failed preview, not audio", async () => {
    globalThis.URL.createObjectURL = () => { throw new Error("must not build a blob URL"); };
    stubFetch(async () => ({
        ok: true,
        headers: { get: (h) => (h.toLowerCase() === "content-type" ? "application/json" : null) },
        text: async () => '{"status":"fallback"}',
        blob: async () => new Blob(['{"status":"fallback"}']),
    }));
    const url = await generateTTSAudioWithVoice("Hi", { provider: "gemini-pro", voice: "Charon" });
    assert.equal(url, null);
});

test("the static picker fallback (catalog unreachable) selects the box default, not a pinned voice", () => {
    // With no catalog, getCurrentVoice() is "" and the speak paths name no
    // voice, so the select must not show (and preview) a pinned Charon.
    const html = readFileSync(new URL("../index.html", import.meta.url), "utf8");
    const selectBody = (id) => {
        const m = html.match(new RegExp(`<select id="${id}"[^>]*>([\\s\\S]*?)</select>`));
        assert.ok(m, `#${id} is in index.html`);
        return m[1];
    };
    for (const id of ["ttsProviderSelect", "ttsVoiceSelect"]) {
        const selected = [...selectBody(id).matchAll(/<option value="([^"]*)"[^>]*\bselected\b/g)]
            .map(m => m[1]);
        assert.deepEqual(selected, [""], `#${id}: only the "" (box default) option is selected`);
    }
});
