// /ws/stt frames on the Portal and the Android WebView (audit rank 52: W24,
// O08-stt-realtime-04). Driven from the SAME fixture the server and Android
// suites read: tests/fixtures/stt_ws_frames.json.
//
//   * stt_error is terminal: capture stops, nothing reconnects;
//   * a switchable error (quota_exceeded first) offers the providers from
//     /stt/catalog that can stream, and picking one restarts the dictation on it,
//     instead of a silent fallback to the legacy path, which hit the same provider;
//   * stt_status provider_override renders as a chip, loading_models as a warming
//     state on the mic button (both were ignored).
//
// The capture is the native Android path (window.AndroidMic), which is exactly
// what the WebView wrapper runs; the socket is a fake.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { installDom, REPO_ROOT } from "./helpers/portal-dom.mjs";

installDom();

const FIXTURE = JSON.parse(readFileSync(resolve(REPO_ROOT, "tests", "fixtures", "stt_ws_frames.json"), "utf8"));
const byName = (name) => FIXTURE.frames.find((f) => f.name === name);

class FakeWS {
    static OPEN = 1;
    static instances = [];
    constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.sent = [];
        this.closed = false;
        FakeWS.instances.push(this);
    }
    send(data) { this.sent.push(JSON.parse(data)); }
    close() {
        if (this.closed) return;
        this.closed = true;
        this.readyState = 3;
        // A browser fires onclose later, never inside close().
        setTimeout(() => { if (this.onclose) this.onclose(); }, 0);
    }
    open() { this.readyState = FakeWS.OPEN; if (this.onopen) return this.onopen(); }
    deliver(frame) { if (this.onmessage) this.onmessage({ data: JSON.stringify(frame) }); }
}
globalThis.WebSocket = FakeWS;
window.WebSocket = FakeWS;

const mic = { started: 0, stopped: 0,
    startAudioStreaming() { this.started++; }, stopAudioStreaming() { this.stopped++; } };
globalThis.AndroidMic = mic;
window.AndroidMic = mic;

const catalogFetches = [];
globalThis.fetch = window.fetch = async (url) => {
    catalogFetches.push(String(url));
    if (String(url).endsWith("/stt/catalog")) {
        return { ok: true, status: 200, json: async () => FIXTURE.catalog };
    }
    throw new Error("unexpected fetch " + url);
};

const mod = await import("../../Portal/modules/stt-stream.js");
const { sttStream } = mod;

const settle = () => new Promise((r) => setTimeout(r, 20));

function ensureTarget() {
    if (!document.getElementById("prompt")) {
        const ta = document.createElement("textarea");
        ta.id = "prompt";
        document.body.appendChild(ta);
        const btn = document.createElement("button");
        btn.id = "ctlMic";
        document.body.appendChild(btn);
    }
    document.getElementById("prompt").value = "";
}

async function startSession(opts = {}) {
    ensureTarget();
    assert.equal(sttStream.isStreaming(), false, "previous session still streaming");
    await sttStream.start("prompt", "ctlMic", opts);
    const ws = FakeWS.instances.at(-1);
    await ws.open();
    return ws;
}

function prompt() { return document.getElementById("sttSwitchPrompt"); }
function chip() { return document.getElementById("sttStatusChip"); }
function dismissPrompt() { const p = prompt(); if (p) p.remove(); }

const errorFrames = FIXTURE.frames.filter((f) => f.expect.kind === "error");

for (const entry of errorFrames) {
    test(`stt_error ${entry.name}: terminal (capture stops, no reconnect)`, async () => {
        dismissPrompt();
        const ws = await startSession();
        // A transcript already landed, so the legacy fallback is out of the picture.
        ws.deliver(byName("delta").frame);
        const sockets = FakeWS.instances.length;
        const stopsBefore = mic.stopped;

        ws.deliver(entry.frame);
        await settle();

        assert.equal(sttStream.isStreaming(), false);
        assert.equal(mic.stopped, stopsBefore + 1, "capture must stop on an stt_error");
        assert.equal(ws.closed, true);
        assert.equal(FakeWS.instances.length, sockets, "an stt_error must not reconnect");
        assert.match(document.getElementById("toast").textContent, new RegExp(entry.frame.message.slice(0, 20).replace(/[.*+?^${}()|[\]\\]/g, "\\$&")));

        if (entry.expect.switchable) {
            const p = prompt();
            assert.ok(p, "a switchable error must offer another provider");
            const offered = [...p.querySelectorAll("button[data-provider]")].map((b) => b.dataset.provider);
            assert.deepEqual(offered, FIXTURE.switch_alternatives[entry.expect.provider]);
        } else {
            assert.equal(prompt(), null, "nothing to switch to: no prompt");
        }
        dismissPrompt();
    });
}

test("a switchable error before any transcript prompts instead of falling back silently", async () => {
    dismissPrompt();
    let fellBack = null;
    const ws = await startSession({ onUnavailable: (reason) => { fellBack = reason; } });
    ws.deliver(byName("elevenlabs_quota_exceeded").frame);
    await settle();
    assert.equal(fellBack, null, "the legacy path would hit the same exhausted provider");
    assert.ok(prompt());
    dismissPrompt();
});

test("a non-switchable error before any transcript keeps the legacy fallback", async () => {
    dismissPrompt();
    let fellBack = null;
    const ws = await startSession({ onUnavailable: (reason) => { fellBack = reason; } });
    ws.deliver(byName("no_provider").frame);
    await settle();
    assert.match(String(fellBack), /stt_error/);
    assert.equal(sttStream.isStreaming(), false);
});

test("picking a provider in the prompt restarts dictation on it", async () => {
    dismissPrompt();
    const ws = await startSession();
    ws.deliver(byName("delta").frame);
    ws.deliver(byName("elevenlabs_quota_exceeded").frame);
    await settle();
    const choice = prompt().querySelector('button[data-provider="openai"]');
    choice.click();
    await settle();
    const next = FakeWS.instances.at(-1);
    assert.notEqual(next, ws, "a new session must start");
    await next.open();
    const start = next.sent.find((m) => m.type === "stt_start");
    assert.equal(start.provider, "openai");
    assert.equal(prompt(), null, "the prompt closes on a pick");
    assert.equal(sttStream.isStreaming(), true);
    sttStream.stop();
    next.deliver(byName("done").frame);
    await settle();
});

test("switchAlternatives follows the fixture", () => {
    assert.equal(typeof mod.switchAlternatives, "function");
    for (const [failed, ids] of Object.entries(FIXTURE.switch_alternatives)) {
        assert.deepEqual(mod.switchAlternatives(FIXTURE.catalog, failed).map((p) => p.id), ids);
    }
    assert.deepEqual(mod.switchAlternatives(null, "openai"), []);
});

test("stt_status provider_override renders as a chip naming both providers", async () => {
    const entry = byName("provider_override");
    const ws = await startSession();
    ws.deliver(entry.frame);
    await settle();
    const c = chip();
    assert.ok(c && !c.hidden, "provider_override must be visible");
    const labels = Object.fromEntries(FIXTURE.catalog.providers.map((p) => [p.id, p.label]));
    assert.match(c.textContent, new RegExp(labels[entry.expect.provider]));
    assert.match(c.textContent, new RegExp(labels[entry.expect.overridden_from]));
    sttStream.stop();
    ws.deliver(byName("done").frame);
    await settle();
    assert.ok(!chip() || chip().hidden, "the chip goes with the session");
});

for (const entry of FIXTURE.frames.filter((f) => f.expect.kind === "status" && f.name !== "provider_override")) {
    test(`stt_status ${entry.name}: warming=${entry.expect.warming}`, async () => {
        const ws = await startSession();
        const btn = document.getElementById("ctlMic");
        if (!entry.expect.warming) ws.deliver(byName("loading_models").frame);
        ws.deliver(entry.frame);
        await settle();
        assert.equal(btn.classList.contains("stt-warming"), entry.expect.warming);
        const c = chip();
        assert.equal(Boolean(c && !c.hidden), entry.expect.warming,
            "the warming chip shows exactly while the on-box models load");
        sttStream.stop();
        ws.deliver(byName("done").frame);
        await settle();
        assert.equal(btn.classList.contains("stt-warming"), false);
    });
}

test("delta, final and done keep their meaning", async () => {
    const ws = await startSession();
    ws.deliver(byName("delta").frame);
    assert.equal(document.getElementById("prompt").value, byName("delta").expect.text);
    ws.deliver(byName("final").frame);
    assert.equal(document.getElementById("prompt").value.trim(), byName("final").expect.text);
    sttStream.stop();
    ws.deliver(byName("done").frame);
    await settle();
    assert.equal(sttStream.isStreaming(), false);
    assert.equal(ws.closed, true);
});
