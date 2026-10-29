// live-session.js: the connect / keepalive / reconnect / teardown state machine
// the three live voice agents share (audit rank 75, P03-live-agents-web-17).
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const { LiveSession } = await import("../../Portal/modules/live-session.js");

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
    close() {
        this.closed = true;
    }
    // test drivers
    open() { this.readyState = 1; this.onopen(); }
    receive(msg) { this.onmessage({ data: JSON.stringify(msg) }); }
    drop(code = 1006, reason = "") { this.readyState = 3; this.onclose({ code, reason }); }
}

function fakeTimers() {
    let now = 1_000_000;
    let seq = 0;
    const pending = new Map();
    return {
        setTimeout: (fn, ms) => { const id = ++seq; pending.set(id, { fn, at: now + ms, every: 0 }); return id; },
        clearTimeout: (id) => pending.delete(id),
        setInterval: (fn, ms) => { const id = ++seq; pending.set(id, { fn, at: now + ms, every: ms }); return id; },
        clearInterval: (id) => pending.delete(id),
        now: () => now,
        advance(ms) {
            const end = now + ms;
            for (;;) {
                const due = [...pending.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                const [id, t] = due;
                now = t.at;
                if (t.every) t.at += t.every; else pending.delete(id);
                t.fn();
            }
            now = end;
        },
        pendingCount: () => pending.size,
    };
}

function makeSession(overrides = {}) {
    FakeSocket.all = [];
    const timers = fakeTimers();
    const events = [];
    let connectCount = 0;
    const session = new LiveSession({
        path: "/ws/gemini-live/",
        logPrefix: "[TEST-LIVE]",
        WebSocketImpl: FakeSocket,
        timers,
        newSessionId: () => "sid-1",
        buildConnectMsg: () => ({ type: "connect", operator: "Ana", voice: "Puck", n: ++connectCount }),
        onMessage: (m) => events.push(["message", m.type]),
        onStatus: (s) => events.push(["status", s]),
        onDropped: () => events.push(["dropped"]),
        onClosed: () => events.push(["closed"]),
        onGiveUp: (exhausted) => events.push(["giveup", exhausted]),
        onResumed: () => events.push(["resumed"]),
        ...overrides,
    });
    return { session, timers, events };
}

test("connect opens the session endpoint and sends the connect message", () => {
    const { session, events } = makeSession();
    session.connect();
    const ws = FakeSocket.all[0];
    assert.equal(ws.url, `ws://${window.location.host}/ws/gemini-live/sid-1`);
    assert.equal(session.isConnecting, true);
    ws.open();
    assert.deepEqual(ws.sent, [{ type: "connect", operator: "Ana", voice: "Puck", n: 1 }]);
    ws.receive({ type: "connected", data: {} });
    assert.deepEqual(events, [
        ["status", "Connecting..."], ["status", "Initializing..."], ["message", "connected"]]);
    assert.equal(session.send({ type: "text_input", text: "hi" }), true);
    assert.deepEqual(ws.sent.at(-1), { type: "text_input", text: "hi" });
});

test("the keepalive pings every 15 s once connected, and a missing pong closes for a reconnect", () => {
    const { session, timers } = makeSession();
    session.connect();
    const ws = FakeSocket.all[0];
    ws.open();
    ws.receive({ type: "connected", data: {} });
    timers.advance(15000);
    assert.deepEqual(ws.sent.at(-1), { type: "ping" });
    ws.receive({ type: "pong" });
    timers.advance(15000);
    assert.equal(ws.closed, false, "a pong arrived within 25 s");
    timers.advance(15000);
    assert.equal(ws.closed, true, "no pong for 30 s: closed so the session reconnects");
});

test("a live socket that drops reconnects to the SAME session with the SAME connect builder, then resumes", () => {
    const { session, timers, events } = makeSession();
    session.connect();
    const first = FakeSocket.all[0];
    first.open();
    first.receive({ type: "connected", data: {} });
    events.length = 0;

    first.drop(1006);
    assert.deepEqual(events, [["dropped"], ["status", "Reconnecting (1/15)..."]]);
    timers.advance(999);
    assert.equal(FakeSocket.all.length, 1, "backoff: 1 s before the first retry");
    timers.advance(1);
    const second = FakeSocket.all[1];
    assert.equal(second.url, first.url, "the server-side session is re-attached, not replaced");
    second.open();
    assert.deepEqual(second.sent, [{ type: "connect", operator: "Ana", voice: "Puck", n: 2 }],
        "a reconnect sends the full connect message (no silent downgrade to server defaults)");
    second.receive({ type: "connected", data: {} });
    assert.deepEqual(events.slice(-2), [["message", "connected"], ["resumed"]]);
    assert.equal(session.reconnectAttempts, 0);
});

test("a reconnect that fails before 'connected' backs off further without a second drop", () => {
    const { session, timers, events } = makeSession();
    session.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ type: "connected", data: {} });
    FakeSocket.all[0].drop();
    timers.advance(1000);
    events.length = 0;
    FakeSocket.all[1].drop();
    assert.deepEqual(events, [["status", "Reconnecting (2/15)..."]], "not dropped again: the mic state is kept");
    timers.advance(1999);
    assert.equal(FakeSocket.all.length, 2);
    timers.advance(1);
    assert.equal(FakeSocket.all.length, 3, "2 s before the second retry");
});

test("a reconnected socket that drops again is a drop (the UI and mic follow it)", () => {
    const { session, timers, events } = makeSession();
    session.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ type: "connected", data: {} });
    FakeSocket.all[0].drop();
    timers.advance(1000);
    FakeSocket.all[1].open();
    FakeSocket.all[1].receive({ type: "connected", data: {} });
    events.length = 0;
    FakeSocket.all[1].drop();
    assert.deepEqual(events[0], ["dropped"]);
});

test("the reconnect budget runs out", () => {
    const { session, timers, events } = makeSession({ maxReconnectAttempts: 2 });
    session.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].receive({ type: "connected", data: {} });
    FakeSocket.all[0].drop();
    timers.advance(1000);
    FakeSocket.all[1].drop();
    timers.advance(2000);
    FakeSocket.all[2].drop();
    assert.deepEqual(events.at(-1), ["giveup", true]);
    timers.advance(60000);
    assert.equal(FakeSocket.all.length, 3);
});

test("the server's upstream reconnect resets the budget", () => {
    const { session } = makeSession();
    session.connect();
    FakeSocket.all[0].open();
    session.reconnectAttempts = 4;
    FakeSocket.all[0].receive({ type: "reconnected", data: { attempt: 1 } });
    assert.equal(session.reconnectAttempts, 0);
});

test("disconnect tells the server, closes, and never reconnects", () => {
    const { session, timers, events } = makeSession();
    session.connect();
    const ws = FakeSocket.all[0];
    ws.open();
    ws.receive({ type: "connected", data: {} });
    session.disconnect();
    assert.deepEqual(ws.sent.at(-1), { type: "disconnect" });
    assert.equal(ws.closed, true);
    assert.equal(session.sessionId, null);
    ws.drop(1005);
    assert.deepEqual(events.at(-1), ["closed"]);
    timers.advance(120000);
    assert.equal(FakeSocket.all.length, 1);
    assert.equal(timers.pendingCount(), 0, "no keepalive or reconnect timer left behind");
});

test("a normal close (1000) ends the session without a reconnect", () => {
    const { session, events } = makeSession();
    session.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].drop(1000);
    assert.deepEqual(events.at(-1), ["closed"]);
});

test("a frame that is not JSON is dropped", () => {
    const { session, events } = makeSession();
    session.connect();
    FakeSocket.all[0].open();
    FakeSocket.all[0].onmessage({ data: "{nope" });
    assert.equal(events.filter(e => e[0] === "message").length, 0);
});
