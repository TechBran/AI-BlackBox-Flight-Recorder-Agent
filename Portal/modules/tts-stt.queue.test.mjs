// Unit tests for the on-box TTS queue PURE helpers (B2, 2026-07-22).
// The web Portal routes on-box (qwen) voices through POST /tts/queue +
// GET /tts/task/{id} polling; these helpers map task states to indicator
// text and drive the poll-backoff / terminal-state logic. They are pure
// (object/number in -> string/number/bool out) so they test without DOM
// or network. DOM shims below exist only so tts-stt.js IMPORTS cleanly
// (it touches window/document at module load) — same preamble as
// tts-stt.voicepicker.test.mjs.
//
// Run: node --test Portal/modules/tts-stt.queue.test.mjs
//  (or: node Portal/modules/tts-stt.queue.test.mjs)
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

const {
    isTtsQueueTerminal, ttsQueuePollDelayMs, formatQueueClock,
    ttsQueueIndicatorText, TTS_QUEUE_POLL_MS, TTS_QUEUE_POLL_MAX_MS,
} = await import("./tts-stt.js");

// ── terminal-state logic ─────────────────────────────────────────────────
test("isTtsQueueTerminal: done/failed/cancelled are terminal", () => {
    assert.equal(isTtsQueueTerminal("done"), true);
    assert.equal(isTtsQueueTerminal("failed"), true);
    assert.equal(isTtsQueueTerminal("cancelled"), true);
});

test("isTtsQueueTerminal: queued/generating/garbage are NOT terminal", () => {
    assert.equal(isTtsQueueTerminal("queued"), false);
    assert.equal(isTtsQueueTerminal("generating"), false);
    assert.equal(isTtsQueueTerminal(""), false);
    assert.equal(isTtsQueueTerminal(undefined), false);
    assert.equal(isTtsQueueTerminal("DONE"), false); // exact server states only
});

// ── poll backoff ─────────────────────────────────────────────────────────
test("ttsQueuePollDelayMs: healthy loop polls at the 1500ms base", () => {
    assert.equal(TTS_QUEUE_POLL_MS, 1500);
    assert.equal(ttsQueuePollDelayMs(0), 1500);
});

test("ttsQueuePollDelayMs: consecutive errors double the delay, capped", () => {
    assert.equal(ttsQueuePollDelayMs(1), 3000);
    assert.equal(ttsQueuePollDelayMs(2), 6000);
    assert.equal(ttsQueuePollDelayMs(3), 12000);
    assert.equal(ttsQueuePollDelayMs(4), TTS_QUEUE_POLL_MAX_MS);
    assert.equal(ttsQueuePollDelayMs(50), TTS_QUEUE_POLL_MAX_MS);
});

test("ttsQueuePollDelayMs: garbage input falls back to the base delay", () => {
    assert.equal(ttsQueuePollDelayMs(-3), 1500);
    assert.equal(ttsQueuePollDelayMs(NaN), 1500);
    assert.equal(ttsQueuePollDelayMs(undefined), 1500);
    assert.equal(ttsQueuePollDelayMs("2"), 6000); // numeric string coerces
});

// ── m:ss clock ───────────────────────────────────────────────────────────
test("formatQueueClock: m:ss formatting", () => {
    assert.equal(formatQueueClock(0), "0:00");
    assert.equal(formatQueueClock(9), "0:09");
    assert.equal(formatQueueClock(75), "1:15");
    assert.equal(formatQueueClock(600), "10:00");
    assert.equal(formatQueueClock(59.9), "0:59"); // floors, never rounds up
});

test("formatQueueClock: garbage input clamps to 0:00", () => {
    assert.equal(formatQueueClock(-5), "0:00");
    assert.equal(formatQueueClock(NaN), "0:00");
    assert.equal(formatQueueClock(undefined), "0:00");
});

// ── state -> indicator-text mapping ──────────────────────────────────────
test("indicator: queued shows N ahead (position - 1)", () => {
    assert.equal(
        ttsQueueIndicatorText({ status: "queued", queue_position: 3 }),
        "Queued — 2 ahead");
    assert.equal(
        ttsQueueIndicatorText({ status: "queued", queue_position: 1 }),
        "Queued — starting next");
    // position 0 (server: no longer active) never goes negative
    assert.equal(
        ttsQueueIndicatorText({ status: "queued", queue_position: 0 }),
        "Queued — starting next");
});

test("indicator: generating shows segment M/K, m:ss elapsed and ~Xs left", () => {
    assert.equal(
        ttsQueueIndicatorText({ status: "generating", subbatch: 2,
                                subbatches_total: 5, elapsed_s: 75.3, eta_s: 30.4 }),
        "Generating audio… segment 2/5 (1:15, ~30s left)");
});

test("indicator: generating before the first sub-batch tick clamps to segment 1", () => {
    assert.equal(
        ttsQueueIndicatorText({ status: "generating", subbatch: 0,
                                subbatches_total: 4, elapsed_s: 2, eta_s: 60 }),
        "Generating audio… segment 1/4 (0:02, ~60s left)");
});

test("indicator: generating without sub-batch info omits the segment part", () => {
    assert.equal(
        ttsQueueIndicatorText({ status: "generating", subbatch: 0,
                                subbatches_total: 0, elapsed_s: 12, eta_s: 0 }),
        "Generating audio… (0:12)");
});

test("indicator: subbatch never exceeds total (last-tick edge)", () => {
    assert.equal(
        ttsQueueIndicatorText({ status: "generating", subbatch: 9,
                                subbatches_total: 5, elapsed_s: 60, eta_s: 5 }),
        "Generating audio… segment 5/5 (1:00, ~5s left)");
});

test("indicator: terminal states", () => {
    assert.equal(ttsQueueIndicatorText({ status: "done" }), "Audio ready");
    assert.equal(ttsQueueIndicatorText({ status: "cancelled" }), "Audio cancelled");
    assert.equal(
        ttsQueueIndicatorText({ status: "failed", error: "GPU busy" }),
        "Audio generation failed: GPU busy");
    assert.equal(
        ttsQueueIndicatorText({ status: "failed" }),
        "Audio generation failed");
});

test("indicator: garbage/unknown input degrades to the preparing label", () => {
    assert.equal(ttsQueueIndicatorText(null), "Preparing audio…");
    assert.equal(ttsQueueIndicatorText(undefined), "Preparing audio…");
    assert.equal(ttsQueueIndicatorText("queued"), "Preparing audio…");
    assert.equal(ttsQueueIndicatorText({ status: "warming-up" }), "Preparing audio…");
});
