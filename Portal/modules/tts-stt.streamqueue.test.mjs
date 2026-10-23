// Auto-TTS (StreamingTTSQueue) under the one-player guard (P02-voice-web-10).
//
// The guard pauses the auto-TTS chunk player when any other audible player
// starts (an older bubble's player, the voice preview, generated media). The
// queue only moved on from 'ended'/'error', so an outside pause left it
// "playing" forever: the indicator never cleared, the reply never got its
// replay player, and its object URLs leaked. An outside pause now counts as the
// listener taking over: the queue stops reading the reply aloud (it never
// restarts over the other player) and still leaves the replay player once
// every chunk is in. stop() and a chunk's natural end are not interruptions.
//
// Run: node --test Portal/modules/tts-stt.streamqueue.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

// ── globals: a fake media element with real pause/ended ordering ──
const listeners = [];
let docMedia = [];
const created = [];   // every new Audio() the module builds

class FakeMedia {
    constructor(src = "") {
        this.src = src;
        this.tagName = "AUDIO";
        this.muted = false;
        this.volume = 1;
        this.paused = true;
        this.ended = false;
        this._l = {};
        created.push(this);
    }
    addEventListener(type, fn) { (this._l[type] = this._l[type] || []).push(fn); }
    _fire(type) {
        const ev = { type, target: this };
        if (typeof this["on" + type] === "function") this["on" + type](ev);
        for (const fn of this._l[type] || []) fn(ev);
        if (docMedia.includes(this)) for (const l of listeners) if (l.type === type) l.fn(ev);
    }
    play() { this.paused = false; this.ended = false; this._fire("play"); return Promise.resolve(); }
    pause() { if (!this.paused) { this.paused = true; this._fire("pause"); } }
    /** Natural end: browsers set paused + ended, fire 'pause', then 'ended'. */
    finish() { this.paused = true; this.ended = true; this._fire("pause"); this._fire("ended"); }
}

function mkNode(className = "") {
    return {
        className, innerHTML: "", textContent: "", style: {}, children: [], removed: false,
        classList: { add() {}, remove() {}, toggle() {} },
        appendChild(c) { this.children.push(c); return c; },
        insertAdjacentElement() {}, addEventListener() {},
        remove() { this.removed = true; },
        // Containers built by attachAudioPlayer look up their own parts.
        querySelector() { return mkNode(); },
        querySelectorAll() { return []; },
        getBoundingClientRect() { return { left: 0, width: 1 }; },
    };
}

/** A chat bubble: no existing player, speak button or controls bar. */
function mkBubble() {
    const b = mkNode("bubble");
    b.querySelector = () => null;
    b.players = () => b.children.filter(c => c.className === "bubble-audio-player");
    return b;
}

globalThis.window = { addEventListener() {}, autoTTSEnabled: true };
globalThis.Audio = FakeMedia;
globalThis.Event = class { constructor(type) { this.type = type; } };
globalThis.localStorage = {
    _s: {}, getItem(k) { return this._s[k] ?? null; },
    setItem(k, v) { this._s[k] = String(v); }, removeItem(k) { delete this._s[k]; },
};
globalThis.document = {
    addEventListener(type, fn, opts) { listeners.push({ type, fn, opts }); },
    getElementById() { return null; }, querySelector() { return null; },
    querySelectorAll(sel) { return sel === "audio, video" ? docMedia : []; },
    createElement() { return mkNode(); },
    head: mkNode(), body: mkNode(),
};
globalThis.FileReader = class {
    readAsDataURL() { setTimeout(() => { this.result = "data:audio/mpeg;base64,eA=="; this.onloadend(); }, 0); }
};
URL.createObjectURL = () => "blob:chunk";
URL.revokeObjectURL = () => {};
globalThis.fetch = async () => ({
    ok: true, headers: { get: () => "audio/mpeg" },
    blob: async () => new Blob(["x"], { type: "audio/mpeg" }), json: async () => ({}),
});

const { startStreamingTTS } = await import("./tts-stt.js");

const TWO = "This is the first sentence of the reply. And this is the second sentence here. ";
const tick = (ms = 20) => new Promise(r => setTimeout(r, ms));

/** Start a queue on a fresh bubble and let both chunks generate + chunk 1 play. */
async function playingQueue() {
    const bubble = mkBubble();
    const q = startStreamingTTS(bubble);
    q.feedChunk(TWO);
    await tick();
    assert.equal(q.audioUrls.length, 2, "both chunks generated");
    assert.equal(q.isPlaying, true);
    assert.equal(q.playbackAudio.paused, false, "chunk 1 is playing");
    return { bubble, q, chunk1: q.playbackAudio };
}

/** Another in-page player starts (an older bubble's <audio>). */
function otherPlayerStarts() {
    const other = new FakeMedia("older.mp3");
    docMedia = [other];
    other.play();
    return other;
}

test("another player pausing auto-TTS mid-reply: no restart, replay player after finalize", async () => {
    const { bubble, q, chunk1 } = await playingQueue();
    const other = otherPlayerStarts();
    assert.equal(chunk1.paused, true, "the guard paused the chunk");
    assert.equal(q.isPlaying, false, "the queue no longer believes it is playing");

    other.finish();
    const before = created.length;
    await q.finalize(TWO);
    await tick();

    assert.equal(created.length, before, "no further chunk started over the listener's choice");
    assert.equal(q.indicator.removed, true, "the 'Playing k/n' indicator is cleared");
    assert.equal(bubble.players().length, 1, "the reply still gets its replay player");
});

test("an interruption after finalize() returned still builds the replay player", async () => {
    const { bubble, q, chunk1 } = await playingQueue();
    await q.finalize(TWO);               // returns while chunk 1 is still playing
    assert.equal(bubble.players().length, 0);
    assert.equal(q.indicator.removed, false);

    otherPlayerStarts();
    await tick();
    assert.equal(chunk1.paused, true);
    assert.equal(q.indicator.removed, true);
    assert.equal(bubble.players().length, 1, "replay player built when the queue is interrupted");
});

test("stop() (a new reply starting) is not an interruption: no replay player", async () => {
    const { bubble, q } = await playingQueue();
    const next = startStreamingTTS(mkBubble());   // stops the running queue
    await tick();
    assert.equal(q.indicator.removed, true);
    assert.equal(bubble.players().length, 0, "a stopped queue attaches nothing");
    next.stop();
});

test("a chunk's natural end still advances to the next chunk and the final player", async () => {
    const { bubble, q, chunk1 } = await playingQueue();
    chunk1.finish();                       // 'pause' then 'ended', ended=true
    const chunk2 = q.playbackAudio;
    assert.notEqual(chunk2, chunk1, "chunk 2 started");
    assert.equal(chunk2.paused, false);
    await q.finalize(TWO);
    chunk2.finish();
    await tick();
    assert.equal(q.indicator.removed, true);
    assert.equal(bubble.players().length, 1);
});
