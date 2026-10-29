// mic-capture.js + pcm-capture-worklet.js: one browser mic capture for the
// voice agents and dictation, on an AudioWorklet with the deprecated
// ScriptProcessorNode as the fallback (audit rank 75, P02-voice-web-12).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, relative } from "node:path";
import { installDom, PORTAL } from "./helpers/portal-dom.mjs";

installDom();

const capture = await import("../../Portal/modules/mic-capture.js");
const workletModule = await import("../../Portal/modules/pcm-capture-worklet.js");

// ── fakes ───────────────────────────────────────────────────────────────
class FakeTrack {
    constructor() { this.stopped = false; }
    stop() { this.stopped = true; }
}

function fakeStream() {
    const track = new FakeTrack();
    return { track, getTracks: () => [track] };
}

class FakeNode {
    constructor(kind) {
        this.kind = kind;
        this.connected = [];
        this.disconnected = false;
    }
    connect(to) { this.connected.push(to); }
    disconnect() { this.disconnected = true; }
}

class FakeAudioContext {
    constructor({ worklet = true } = {}) {
        this.sampleRate = 48000;
        this.state = "suspended";
        this.destination = new FakeNode("destination");
        this.modules = [];
        if (worklet) {
            this.audioWorklet = { addModule: async (url) => { this.modules.push(url); } };
        }
    }
    async resume() { this.state = "running"; }
    createMediaStreamSource(stream) {
        const node = new FakeNode("source");
        node.stream = stream;
        return node;
    }
    createScriptProcessor(size, inCh, outCh) {
        const node = new FakeNode("script");
        node.size = size;
        node.channels = [inCh, outCh];
        this.script = node;
        return node;
    }
}

class FakeWorkletNode extends FakeNode {
    constructor(context, name, options) {
        super("worklet");
        this.name = name;
        this.options = options;
        this.posted = [];
        this.port = { onmessage: null, postMessage: (m) => this.posted.push(m) };
        FakeWorkletNode.last = this;
    }
}

function withMedia(getUserMedia) {
    Object.defineProperty(navigator, "mediaDevices", {
        value: { getUserMedia }, configurable: true,
    });
}

// ── getMicStream ────────────────────────────────────────────────────────
test("a busy mic is retried with growing delays, then opened", async () => {
    const delays = [];
    let attempts = 0;
    const stream = fakeStream();
    withMedia(async () => {
        attempts++;
        if (attempts < 3) throw Object.assign(new Error("busy"), { name: "NotReadableError" });
        return stream;
    });
    const got = await capture.getMicStream({ sleep: async (ms) => { delays.push(ms); } });
    assert.equal(got, stream);
    assert.deepEqual(delays, [500, 1000]);
});

test("getUserMedia failures become client-facing messages (and can be overridden)", async () => {
    withMedia(async () => { throw Object.assign(new Error("no"), { name: "NotAllowedError" }); });
    await assert.rejects(capture.getMicStream(), /Microphone permission denied\./);
    await assert.rejects(capture.getMicStream({ messages: { NotAllowedError: "Please allow the mic." } }),
        /Please allow the mic\./);
    withMedia(async () => { throw Object.assign(new Error("odd"), { name: "AbortError" }); });
    await assert.rejects(capture.getMicStream(), /Microphone error: odd/);
    withMedia(async () => { throw Object.assign(new Error("busy"), { name: "NotReadableError" }); });
    await assert.rejects(capture.getMicStream({ retries: 1, sleep: async () => {} }), /Microphone is busy\./);
});

test("without getUserMedia (plain http) the mic is unsupported", async () => {
    Object.defineProperty(navigator, "mediaDevices", { value: undefined, configurable: true });
    await assert.rejects(capture.getMicStream(), /Requires HTTPS or localhost/);
});

// ── openMicCapture ──────────────────────────────────────────────────────
test("capture runs on an AudioWorklet when the engine has one, and close() releases everything", async () => {
    const stream = fakeStream();
    withMedia(async () => stream);
    globalThis.AudioWorkletNode = FakeWorkletNode;
    const ctx = new FakeAudioContext();
    const frames = [];
    const cap = await capture.openMicCapture({ context: ctx, frameSize: 4096, onFrame: (f, rate) => frames.push([f, rate]) });
    assert.equal(cap.worklet, true);
    assert.equal(ctx.state, "running", "a suspended context is resumed");
    assert.equal(ctx.modules.length, 1);
    assert.match(ctx.modules[0], /pcm-capture-worklet\.js$/);
    const node = FakeWorkletNode.last;
    assert.equal(node.name, workletModule.PROCESSOR_NAME);
    assert.deepEqual(node.options.processorOptions, { frameSize: 4096 });
    assert.deepEqual(node.connected, [ctx.destination]);

    const frame = new Float32Array(4096).fill(0.1);
    node.port.onmessage({ data: frame });
    assert.equal(frames.length, 1);
    assert.equal(frames[0][0], frame);
    assert.equal(frames[0][1], 48000);

    cap.close();
    assert.deepEqual(node.posted, ["stop"]);
    assert.equal(node.disconnected, true);
    assert.equal(stream.track.stopped, true);
    cap.close();  // idempotent

    // A second capture on the same context does not reload the module.
    await capture.openMicCapture({ context: ctx, frameSize: 4096, onFrame: () => {} });
    assert.equal(ctx.modules.length, 1);
    delete globalThis.AudioWorkletNode;
});

test("an engine without AudioWorklet falls back to ScriptProcessorNode", async () => {
    const stream = fakeStream();
    withMedia(async () => stream);
    const ctx = new FakeAudioContext({ worklet: false });
    const frames = [];
    const cap = await capture.openMicCapture({ context: ctx, frameSize: 8192, onFrame: (f) => frames.push(f) });
    assert.equal(cap.worklet, false);
    assert.equal(ctx.script.size, 8192);
    const samples = new Float32Array(8192).fill(0.2);
    ctx.script.onaudioprocess({ inputBuffer: { getChannelData: () => samples } });
    assert.deepEqual(frames, [samples]);
    cap.close();
    assert.equal(ctx.script.onaudioprocess, null);
    assert.equal(stream.track.stopped, true);
});

test("a worklet that fails to load falls back too", async () => {
    withMedia(async () => fakeStream());
    globalThis.AudioWorkletNode = FakeWorkletNode;
    const ctx = new FakeAudioContext();
    ctx.audioWorklet.addModule = async () => { throw new Error("blocked by CSP"); };
    const cap = await capture.openMicCapture({ context: ctx, onFrame: () => {} });
    assert.equal(cap.worklet, false);
    cap.close();
    delete globalThis.AudioWorkletNode;
});

// ── the worklet processor ───────────────────────────────────────────────
class FakeProcessorBase {
    constructor() {
        this.posted = [];
        this.port = { onmessage: null, postMessage: (m, transfer) => this.posted.push([m, transfer]) };
    }
}

test("the processor collects 128-sample render quanta into whole frames, in order", () => {
    const Processor = workletModule.createProcessorClass(FakeProcessorBase);
    const p = new Processor({ processorOptions: { frameSize: 300 } });
    let next = 0;
    const quantum = () => [[Float32Array.from({ length: 128 }, () => next++)]];
    for (let i = 0; i < 5; i++) assert.equal(p.process(quantum()), true);   // 640 samples
    assert.equal(p.posted.length, 2);
    const [first, transfer] = p.posted[0];
    assert.equal(first.length, 300);
    assert.equal(first[0], 0);
    assert.equal(first[299], 299);
    assert.equal(transfer[0], first.buffer, "the frame's buffer is transferred, not copied");
    assert.equal(p.posted[1][0][0], 300);
    assert.equal(p.filled, 40, "the remainder waits for the next frame");
});

test("the processor idles without input and ends on 'stop'", () => {
    const Processor = workletModule.createProcessorClass(FakeProcessorBase);
    const p = new Processor({});
    assert.equal(p.frameSize, 4096);
    assert.equal(p.process([[]]), true);
    p.port.onmessage({ data: "stop" });
    assert.equal(p.process([[new Float32Array(128)]]), false);
});

test("every other microphone in the Portal opens with the one mic profile", () => {
    // Dictation's and the Gemini recorder's MediaRecorder paths and the Voice
    // Lab kept their own copies of these constraints (audit P02-voice-web-12).
    const offenders = [];
    (function walk(dir) {
        for (const name of readdirSync(dir)) {
            if (["vendor", "uploads", "artifacts", "node_modules"].includes(name)) continue;
            const p = join(dir, name);
            if (statSync(p).isDirectory()) walk(p);
            else if (name.endsWith(".js") && !p.endsWith(join("modules", "mic-capture.js"))) {
                const code = readFileSync(p, "utf8");
                for (const m of code.matchAll(/getUserMedia\(\s*([^)]*?)\s*\)/g)) {
                    if (m[1] !== "MIC_CONSTRAINTS") offenders.push(`${relative(PORTAL, p)}: getUserMedia(${m[1].slice(0, 40)})`);
                }
            }
        }
    })(PORTAL);
    assert.deepEqual(offenders, []);
});
