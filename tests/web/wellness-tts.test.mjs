// A health reply is never read aloud (M7 wave 3b, contract (e); rules W26,
// W31, W32). The Portal already keeps a health reply's speak buttons and
// auto-TTS from asking (wellness-chat-send / wellness-chat-bubbles suites);
// the box guards every client too: POST /tts, /tts/batch and /tts/queue
// answer 403 {"error": "wellness_text"} for a text that recalls one of the
// operator's health results. Whichever TTS call gets that answer, the Portal
// says the same thing the speak buttons say (WELLNESS_SPEECH_OFF), never
// retries (no /tts/batch after a refused /tts/queue, no browser speech
// fallback), plays nothing and logs nothing about the text. Any other
// failure is handled as before.
//
// The fixture numbers are the synthetic wellness-fixture operator's.
//
// Run: bash scripts/run_web_tests.sh
import test from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();
localStorage.setItem("bbx_operator", "wellness-fixture");

// The browser's own speech (speak()'s fallback), recorded.
const browserSpoke = [];
globalThis.SpeechSynthesisUtterance = window.SpeechSynthesisUtterance = class { constructor(text) { this.text = text; } };
globalThis.speechSynthesis = window.speechSynthesis = {
    speak(u) { browserSpoke.push(u.text); setTimeout(() => u.onend && u.onend(), 0); },
    cancel() {},
};

const tts = await import("../../Portal/modules/tts-stt.js");
const { WELLNESS_SPEECH_OFF } = await import("../../Portal/modules/wellness-section.js");
const bubbles = await import("../../Portal/modules/chat-bubbles.js");

const TEXT = "Your sleeping heart rate was 47 bpm.";
const REFUSAL = { error: "wellness_text", detail: "This text repeats your health data, so it is not sent to a voice." };

const respond = (status, body, type = "application/json") => new Response(
    typeof body === "string" ? body : JSON.stringify(body), { status, headers: { "Content-Type": type } });

/** The box: each route's answer ({status, body}); records every call. */
function stubBox(routes) {
    const calls = [];
    globalThis.fetch = window.fetch = async (url, opts = {}) => {
        const u = String(url);
        calls.push({ url: u, opts });
        const path = u.split("?")[0];
        const r = routes[path];
        if (!r) return respond(404, { detail: "not here" });
        return respond(r.status, r.body, r.type);
    };
    return calls;
}
const ttsCalls = (calls) => calls.filter((c) => /^\/tts(\/batch|\/queue)?$/.test(c.url));

function useVoice(t, voice) {
    localStorage.setItem("bb_operator_voices", JSON.stringify({ "wellness-fixture": voice }));
    t.after(() => localStorage.removeItem("bb_operator_voices"));
}

function bubbleWithButton() {
    const bubble = document.createElement("div");
    bubble.className = "bubble assistant";
    bubble.innerHTML = '<span class="bubble-text"></span><div class="bubble-controls"><button class="bubble-btn speak-btn" title="Listen"></button></div>';
    bubble.querySelector(".bubble-text").textContent = TEXT;
    document.getElementById("history").appendChild(bubble);
    return { bubble, btn: bubble.querySelector(".speak-btn") };
}

function captureLogs(t) {
    const logged = [];
    const originals = {};
    for (const level of ["log", "info", "debug", "warn", "error"]) {
        originals[level] = console[level];
        console[level] = (...args) => logged.push(args.map((a) => (typeof a === "string" ? a : String(a && a.message || a))).join(" "));
    }
    t.after(() => Object.assign(console, originals));
    return logged;
}

const toastText = () => document.getElementById("toast").textContent;

function reset(t) {
    document.getElementById("history").innerHTML = "";
    document.getElementById("toast").textContent = "";
    browserSpoke.length = 0;
    t.after(() => { document.getElementById("history").innerHTML = ""; });
}

test("the speech message is the one the speak buttons say", () => {
    assert.equal(WELLNESS_SPEECH_OFF, "Health replies aren't read aloud: the voice service would get your health data.");
    assert.equal(bubbles.WELLNESS_SPEECH_OFF, WELLNESS_SPEECH_OFF);
});

test("a speak button's /tts/batch refused as wellness_text: the message, no audio, no retry, nothing logged", async (t) => {
    reset(t);
    useVoice(t, "openai:alloy");
    const logged = captureLogs(t);
    const calls = stubBox({ "/tts/batch": { status: 403, body: REFUSAL } });
    const { bubble, btn } = bubbleWithButton();
    await tts.speakToBubble(TEXT, bubble, btn);
    assert.equal(ttsCalls(calls).length, 1, "asked once, never again");
    assert.equal(toastText(), WELLNESS_SPEECH_OFF);
    assert.equal(bubble.querySelector(".bubble-audio-player"), null, "nothing to play");
    assert.equal(bubble.dataset.audioKey, undefined, "nothing cached");
    assert.equal(btn.classList.contains("bubble-btn-loading"), false, "the button is idle again");
    assert.deepEqual(browserSpoke, []);
    assert.deepEqual(logged.filter((l) => l.includes("47") || l.includes("repeats your health")), []);
});

test("an on-box voice's /tts/queue refused: the message, and no fallback to /tts/batch", async (t) => {
    reset(t);
    useVoice(t, "qwen:fixture-voice");
    captureLogs(t);
    const calls = stubBox({ "/tts/queue": { status: 403, body: REFUSAL }, "/tts/batch": { status: 500, body: { detail: "never" } } });
    const { bubble, btn } = bubbleWithButton();
    await tts.speakToBubble(TEXT, bubble, btn);
    assert.deepEqual(ttsCalls(calls).map((c) => c.url), ["/tts/queue"]);
    assert.equal(toastText(), WELLNESS_SPEECH_OFF);
    assert.equal(bubble.querySelector(".bubble-audio-player"), null);
});

test("speak(): a refused /tts/batch never falls back to the browser's own voice", async (t) => {
    reset(t);
    useVoice(t, "openai:alloy");
    captureLogs(t);
    const calls = stubBox({ "/tts/batch": { status: 403, body: REFUSAL } });
    await tts.speak(TEXT, document.createElement("button"));
    assert.equal(ttsCalls(calls).length, 1);
    assert.deepEqual(browserSpoke, [], "no speech synthesis of the health text");
    assert.equal(toastText(), WELLNESS_SPEECH_OFF);
});

test("Listen to reasoning: a refused /tts/batch says why instead of 'failed'", async (t) => {
    reset(t);
    useVoice(t, "openai:alloy");
    captureLogs(t);
    const calls = stubBox({ "/tts/batch": { status: 403, body: REFUSAL } });
    const host = document.createElement("div");
    host.innerHTML = '<div class="streaming-thinking-panel"><div class="thinking-content"></div><button data-bubble-action="thinking-speak"></button></div>';
    host.querySelector(".thinking-content").textContent = "The tool said 47 bpm.";
    document.getElementById("history").appendChild(host);
    const btn = host.querySelector("button");
    await tts.speakThinkingContent(btn);
    assert.equal(ttsCalls(calls).length, 1);
    assert.equal(toastText(), WELLNESS_SPEECH_OFF);
    assert.equal(btn.classList.contains("thinking-btn-loading"), false);
    assert.equal(host.querySelector(".thinking-audio-player"), null);
});

test("a single /tts call refused: no audio URL, the message", async (t) => {
    reset(t);
    const logged = captureLogs(t);
    const calls = stubBox({ "/tts": { status: 403, body: REFUSAL } });
    assert.equal(await tts.generateTTSAudioWithVoice(TEXT, { provider: "openai", voice: "alloy" }), null);
    assert.equal(ttsCalls(calls).length, 1);
    assert.equal(toastText(), WELLNESS_SPEECH_OFF);
    assert.deepEqual(logged.filter((l) => l.includes("repeats your health")), [], "the refusal body is not logged");
});

// ── Positive controls: every other failure is what it was ──────────────────

test("any other /tts/batch failure still reports 'Audio generation failed'", async (t) => {
    reset(t);
    useVoice(t, "openai:alloy");
    captureLogs(t);
    stubBox({ "/tts/batch": { status: 403, body: { error: "forbidden" } } });
    const { bubble, btn } = bubbleWithButton();
    await tts.speakToBubble("Hello there.", bubble, btn);
    assert.match(toastText(), /^Audio generation failed/);
});

test("speak()'s browser fallback still covers an ordinary failure", async (t) => {
    reset(t);
    useVoice(t, "openai:alloy");
    captureLogs(t);
    stubBox({ "/tts/batch": { status: 500, body: { detail: "down" } } });
    await tts.speak("Hello there.", document.createElement("button"));
    assert.deepEqual(browserSpoke, ["Hello there."]);
});

test("a refused /tts/queue that is not wellness_text is still an error, not a fallback", async (t) => {
    reset(t);
    useVoice(t, "qwen:fixture-voice");
    captureLogs(t);
    const calls = stubBox({ "/tts/queue": { status: 400, body: { detail: "unknown voice" } } });
    const { bubble, btn } = bubbleWithButton();
    await tts.speakToBubble("Hello there.", bubble, btn);
    assert.deepEqual(ttsCalls(calls).map((c) => c.url), ["/tts/queue"]);
    assert.equal(toastText(), "Audio generation failed: unknown voice");
});
