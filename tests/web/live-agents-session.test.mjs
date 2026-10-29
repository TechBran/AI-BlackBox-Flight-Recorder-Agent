// The three live voice agents on live-session.js: the first connect and every
// reconnect send the SAME connect message, built once per module (a reconnect
// used to rebuild a thinner one and silently downgrade the session, T14 F1;
// audit rank 75, P03-live-agents-web-17).
//
// Run: cd tests/web && npm ci && node --test
import test, { mock } from "node:test";
import assert from "node:assert/strict";
import { installDom, loadPurify, loadMarked } from "./helpers/portal-dom.mjs";

installDom();
loadPurify();
loadMarked();

class FakeSocket {
    static all = [];
    constructor(url) {
        this.url = url;
        this.readyState = 0;
        this.sent = [];
        this.closed = false;
        FakeSocket.all.push(this);
    }
    send(data) {
        if (this.readyState !== 1) throw new Error("not open");
        this.sent.push(JSON.parse(data));
    }
    close() { this.closed = true; }
    open() { this.readyState = 1; this.onopen(); }
    receive(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
    drop(code = 1006) { this.readyState = 3; this.onclose({ code, reason: "" }); }
}
window.WebSocket = FakeSocket;
globalThis.WebSocket = FakeSocket;

function select(id, value) {
    document.body.insertAdjacentHTML("beforeend",
        `<select id="${id}"><option value="${value}" selected>${value}</option></select>`);
}

const AGENTS = [
    {
        name: "gemini-live", path: "/ws/gemini-live/",
        setup: () => { select("geminiVoiceSelect", "Puck"); select("geminiModelSelect", "gemini-model-x"); },
        expect: { type: "connect", operator: "Ana", voice: "Puck", model: "gemini-model-x" },
    },
    {
        name: "grok-live", path: "/ws/grok-live/",
        setup: () => {
            select("grokVoiceSelect", "Rex"); select("grokModelSelect", "grok-model-x");
            select("grokReasoningSelect", "high");
        },
        expect: { type: "connect", operator: "Ana", voice: "Rex", model: "grok-model-x", reasoning_effort: "high" },
    },
    {
        name: "gpt-realtime", path: "/ws/realtime/",
        setup: () => {
            select("realtimeVoiceSelect", "marin"); select("realtimeModelSelect", "gpt-model-x");
            select("realtimeNoiseSelect", "far_field");
        },
        expect: { type: "connect", operator: "Ana", voice: "marin", model: "gpt-model-x", noise_reduction: "far_field" },
    },
];

for (const agent of AGENTS) {
    test(`${agent.name}: a reconnect re-attaches the session with the first connect's message`, async () => {
        mock.timers.enable({ apis: ["setTimeout", "setInterval"] });
        try {
            agent.setup();
            const mod = await import(`../../Portal/modules/${agent.name}.js`);
            FakeSocket.all = [];
            await mod.connect("Ana");
            const first = FakeSocket.all[0];
            assert.ok(first.url.includes(agent.path), first.url);
            first.open();
            assert.deepEqual(first.sent[0], agent.expect);
            first.receive({ type: "connected", data: { voice: agent.expect.voice } });

            first.drop(1006);                  // network blip
            mock.timers.tick(1000);            // first backoff
            const second = FakeSocket.all[1];
            assert.equal(second.url, first.url, "same server-side session id");
            second.open();
            assert.deepEqual(second.sent[0], agent.expect,
                "the reconnect sends exactly the first connect's message");

            mod.disconnect();
            assert.deepEqual(second.sent.at(-1), { type: "disconnect" });
            second.drop(1005);
            mock.timers.tick(60000);
            assert.equal(FakeSocket.all.length, 2, "no reconnect after a disconnect");
        } finally {
            mock.timers.reset();
        }
    });
}
