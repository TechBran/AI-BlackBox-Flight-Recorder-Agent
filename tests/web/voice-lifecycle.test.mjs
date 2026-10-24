// Microphones, voice sessions and modals get a lifecycle (audit rank 66/67:
// P02-voice-web-02, P02-voice-web-04, P03-live-agents-web-03,
// P06-comms-web-04, P01-chat-core-web-05).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, loadPurify, loadMarked, PORTAL } from "./helpers/portal-dom.mjs";
import { stripComments } from "./helpers/sink-scan.mjs";

installDom();
loadPurify();
loadMarked();

// Record what tts-stt registers on window BEFORE it loads.
const windowListeners = [];
const realAdd = window.addEventListener.bind(window);
window.addEventListener = (type, fn, opts) => { windowListeners.push(type); return realAdd(type, fn, opts); };
const tts = await import("../../Portal/modules/tts-stt.js");
window.addEventListener = realAdd;

const arbiter = await import("../../Portal/modules/mic-arbiter.js");

// ── mic arbiter ──────────────────────────────────────────────────────────
test("claiming the mic stops the previous owner, once", async () => {
    const released = [];
    await arbiter.claimMic("dictation", () => released.push("dictation"));
    await arbiter.claimMic("dictation", () => released.push("dictation-again"));
    assert.deepEqual(released, [], "re-claiming your own mic stops nothing");
    await arbiter.claimMic("realtime", () => released.push("realtime"));
    assert.deepEqual(released, ["dictation-again"], "the owner's latest release callback runs, once");
    assert.equal(arbiter.currentMicOwner(), "realtime");
    arbiter.releaseMic("dictation");
    assert.equal(arbiter.currentMicOwner(), "realtime", "a stale release cannot drop another owner's claim");
    arbiter.releaseMic("realtime");
    assert.equal(arbiter.currentMicOwner(), null);
});

test("a failing release never blocks the new owner", async () => {
    await arbiter.claimMic("gemini-recorder", () => { throw new Error("already gone"); });
    await arbiter.claimMic("grok-live", null);
    assert.equal(arbiter.currentMicOwner(), "grok-live");
    arbiter.releaseMic("grok-live");
});

test("native stream callbacks go to the module that bound last; a stale unbind cannot cut its feed", () => {
    const got = [];
    arbiter.bindNativeStream("realtime", { onNativeAudioChunk: (b) => got.push(["realtime", b]) });
    window.onNativeAudioChunk("a");
    arbiter.bindNativeStream("stt-stream", { onNativeAudioChunk: (b) => got.push(["stt", b]) });
    window.onNativeAudioChunk("b");
    arbiter.unbindNativeStream("realtime");      // realtime's late stop
    window.onNativeAudioChunk("c");
    arbiter.unbindNativeStream("stt-stream");
    window.onNativeAudioChunk("d");               // no owner: dropped, no throw
    assert.deepEqual(got, [["realtime", "a"], ["stt", "b"], ["stt", "c"]]);
    for (const name of arbiter.NATIVE_STREAM_CALLBACKS) assert.equal(typeof window[name], "function");
});

test("no module reads the never-assigned mic globals or overwrites the native callbacks", () => {
    const offenders = [];
    for (const name of readdirSync(resolve(PORTAL, "modules"))) {
        if (!name.endsWith(".js") || name === "mic-arbiter.js") continue;
        const src = stripComments(readFileSync(resolve(PORTAL, "modules", name), "utf8"));
        if (/window\.(micOn|stopSTT|geminiRecording)\b/.test(src)) offenders.push(`${name}: reads window.micOn/stopSTT/geminiRecording`);
        if (/window\.onNative(StreamStart|AudioChunk|StreamStop|StreamError)\s*=(?!=)/.test(src)) {
            offenders.push(`${name}: assigns a native stream callback directly`);
        }
    }
    assert.deepEqual(offenders, []);
});

test("every mic user goes through the arbiter", () => {
    for (const name of ["tts-stt.js", "stt-stream.js", "gpt-realtime.js", "gemini-live.js", "grok-live.js", "gemini-recorder.js"]) {
        const src = readFileSync(resolve(PORTAL, "modules", name), "utf8");
        assert.match(src, /from '\.\/mic-arbiter\.js'/, name);
    }
});

// ── Grok native audio ────────────────────────────────────────────────────
test("Grok resamples the 24 kHz native mic chunk to the bridge's 16 kHz", async () => {
    const { resampleNativeChunkForGrok } = await import("../../Portal/modules/grok-live.js");
    const samples = 2400;                       // 100 ms at 24 kHz
    const pcm = new Int16Array(samples);
    for (let i = 0; i < samples; i++) pcm[i] = Math.round(8000 * Math.sin((2 * Math.PI * 440 * i) / 24000));
    const b64 = Buffer.from(pcm.buffer).toString("base64");
    const out = Buffer.from(resampleNativeChunkForGrok(b64), "base64");
    assert.equal(out.length / 2, 1600, "100 ms at 16 kHz");
    const src = readFileSync(resolve(PORTAL, "modules", "grok-live.js"), "utf8");
    assert.match(src, /data: resampleNativeChunkForGrok\(base64Audio\)/, "the native path sends the resampled chunk");
});

// ── TTS ──────────────────────────────────────────────────────────────────
test("backgrounding the tab does not cancel TTS; unload still does", () => {
    assert.ok(windowListeners.includes("beforeunload"));
    assert.ok(!windowListeners.includes("visibilitychange"),
        "a phone lock / app switch must not throw away the audio being generated");
});

test("the reasoning panel's Listen button has a function to call", () => {
    assert.equal(typeof window.speakThinkingContent, "function");
    assert.equal(window.speakThinkingContent, tts.speakThinkingContent);
});

// ── Escape runs each modal's own close path ──────────────────────────────
test("Escape closes the Voice Agents modal through its own close handler", async () => {
    document.body.insertAdjacentHTML("beforeend", `
        <div id="voiceAgentsModal" class="modal">
          <div class="modal-card"><div class="modal-head">
            <button id="btnCloseVoiceAgents" class="btn modal-close" type="button">✕</button>
          </div></div>
        </div>
        <div id="smsInboxModal" class="modal">
          <div class="modal-head"><button id="btnCloseSMS" class="btn">✕</button></div>
        </div>
        <div id="confirmClearModal" class="modal">
          <button id="btnCancelClear" class="btn">Cancel</button>
        </div>
        <div id="bareModal" class="modal"><p>no controls</p></div>`);
    const clicked = [];
    for (const id of ["btnCloseVoiceAgents", "btnCloseSMS", "btnCancelClear"]) {
        document.getElementById(id).addEventListener("click", () => clicked.push(id));
    }
    const { initVoiceAgentsModal } = await import("../../Portal/modules/voice-agents-modal.js");
    initVoiceAgentsModal();   // binds closeModal (disconnects every provider) to .modal-close
    const ui = await import("../../Portal/modules/ui-setup.js");
    ui.setupKeyboardShortcuts();

    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true }));

    assert.deepEqual(clicked.sort(), ["btnCancelClear", "btnCloseSMS", "btnCloseVoiceAgents"]);
    for (const id of ["voiceAgentsModal", "smsInboxModal", "confirmClearModal", "bareModal"]) {
        assert.ok(document.getElementById(id).classList.contains("hide"), `${id} is hidden`);
    }
});

test("findModalCloseControl prefers the modal's own close control", async () => {
    const { findModalCloseControl } = await import("../../Portal/modules/ui-setup.js");
    document.body.insertAdjacentHTML("beforeend", `
        <div id="m1" class="modal"><div class="modal-head"><button id="btnCloseTimeline">✕</button></div>
          <button id="btnCloseSnapshotPopup" class="snapshot-popup-close">x</button></div>
        <div id="m2" class="modal"><div class="modal-head"><button class="btn updates-log-close">✕</button></div></div>`);
    assert.equal(findModalCloseControl(document.getElementById("m1")).id, "btnCloseTimeline");
    assert.ok(findModalCloseControl(document.getElementById("m2")).classList.contains("updates-log-close"));
});
