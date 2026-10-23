// One player at a time (audit P02-voice-web-10, rank 39).
//
// Bubble players, auto-TTS, the voice preview and generated media could all
// play at once: nothing paused the previous element when a new one started.
// tts-stt.js now registers ONE capture-phase 'play' listener on document that
// pauses every other audible <audio>/<video>, and trackDetachedPlayer() covers
// the new Audio() players that never enter the DOM (their events never reach
// document). Muted media (a looping muted video) neither pauses nor is paused.
//
// Run: node --test Portal/modules/tts-stt.overlap.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

// ── minimal globals so tts-stt.js (+ its imports) load cleanly ──
const listeners = [];
let docMedia = [];
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
    addEventListener(type, fn, opts) { listeners.push({ type, fn, opts }); },
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll(sel) { return sel === "audio, video" ? docMedia : []; },
    createElement() { return { ...minimalNode }; },
    head: minimalNode, body: minimalNode,
};
globalThis.fetch = () => Promise.reject(new Error("no network in unit test"));

const { pauseOtherPlayers, pauseAllPlayers, trackDetachedPlayer } = await import("./tts-stt.js");

/** A media element: play()/pause() flip `paused` and fire their events. */
class FakeMedia {
    constructor(tag = "AUDIO", { muted = false, volume = 1, playing = false } = {}) {
        this.tagName = tag;
        this.muted = muted;
        this.volume = volume;
        this.paused = !playing;
        this.pauseCalls = 0;
        this._l = {};
    }
    addEventListener(type, fn) { (this._l[type] = this._l[type] || []).push(fn); }
    _fire(type) {
        for (const fn of this._l[type] || []) fn({ type, target: this });
        // In-DOM elements: the event reaches document's capture listener.
        if (docMedia.includes(this)) {
            for (const l of listeners) if (l.type === type) l.fn({ type, target: this });
        }
    }
    play() { this.paused = false; this._fire("play"); return Promise.resolve(); }
    pause() { if (!this.paused) { this.paused = true; this.pauseCalls++; this._fire("pause"); } }
}

test("exactly one capture-phase 'play' listener is registered on document", () => {
    const play = listeners.filter(l => l.type === "play");
    assert.equal(play.length, 1);
    const opts = play[0].opts;
    assert.ok(opts === true || (opts && opts.capture === true), "must be capture phase");
});

test("a bubble starting pauses every other playing element, not itself", () => {
    const a = new FakeMedia("AUDIO", { playing: true });
    const b = new FakeMedia("AUDIO");
    const v = new FakeMedia("VIDEO", { playing: true });
    const idle = new FakeMedia("AUDIO");
    docMedia = [a, b, v, idle];
    b.play();
    assert.equal(b.paused, false);
    assert.equal(a.paused, true);
    assert.equal(v.paused, true);
    assert.equal(idle.pauseCalls, 0, "an already-paused element is left alone");
});

test("muted media never pauses others and is never paused", () => {
    const narration = new FakeMedia("AUDIO", { playing: true });
    const loop = new FakeMedia("VIDEO", { muted: true });
    docMedia = [narration, loop];
    loop.play();
    assert.equal(narration.paused, false, "a muted video starting leaves narration playing");
    const zero = new FakeMedia("AUDIO", { volume: 0, playing: true });
    const other = new FakeMedia("AUDIO");
    docMedia = [zero, other, loop];
    other.play();
    assert.equal(zero.paused, false, "a silent (volume 0) element is not paused");
    assert.equal(loop.paused, false, "a muted element is not paused");
});

test("detached players (new Audio()) join the guard both ways", () => {
    const bubble = new FakeMedia("AUDIO", { playing: true });
    docMedia = [bubble];
    const preview = trackDetachedPlayer(new FakeMedia("AUDIO"));
    preview.play();                       // never in the DOM: its own listener runs the guard
    assert.equal(bubble.paused, true, "the detached preview paused the bubble");
    assert.equal(preview.paused, false);

    bubble.play();                        // the bubble starts again from the DOM
    assert.equal(preview.paused, true, "the bubble paused the detached preview");
    assert.equal(preview.pauseCalls, 1);
    bubble.play();
    assert.equal(preview.pauseCalls, 1, "a paused detached player is not paused twice");
});

test("trackDetachedPlayer is idempotent and tolerates non-media objects", () => {
    const p = new FakeMedia("AUDIO");
    assert.equal(trackDetachedPlayer(p), p);
    assert.equal(trackDetachedPlayer(p), p);
    assert.equal(p._l.play.length, 1, "registered once");
    const plain = { paused: true };
    assert.equal(trackDetachedPlayer(plain), plain, "no addEventListener: returned untouched");
    assert.equal(trackDetachedPlayer(null), null);
});

test("pauseOtherPlayers returns how many it paused", () => {
    const a = new FakeMedia("AUDIO", { playing: true });
    const b = new FakeMedia("AUDIO", { playing: true });
    const c = new FakeMedia("AUDIO", { playing: true });
    docMedia = [a, b, c];
    assert.equal(pauseOtherPlayers(a), 2);
    assert.equal(a.paused, false);
});

test("pauseAllPlayers (a live voice agent connecting) pauses every audible player", () => {
    const bubble = new FakeMedia("AUDIO", { playing: true });
    const loop = new FakeMedia("VIDEO", { muted: true, playing: true });
    docMedia = [bubble, loop];
    assert.equal(pauseAllPlayers(), 1);
    assert.equal(bubble.paused, true, "the in-page bubble player is paused");
    assert.equal(loop.paused, false, "muted media is left alone");

    const preview = trackDetachedPlayer(new FakeMedia("AUDIO"));
    preview.play();
    assert.equal(pauseAllPlayers(), 1);
    assert.equal(preview.paused, true, "a playing detached player is paused too");
});
