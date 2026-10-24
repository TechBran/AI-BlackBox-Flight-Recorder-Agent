// One poller contract for the Portal (audit P01-chat-core-web-21, rank 67).
//
// Five page-lifetime setInterval pollers (/tasks/list every 2 s, /health, SMS
// unread, three media sweeps, the overlay check) kept a backgrounded tab or
// Android WebView hammering the box, stacked a second request on a slow one,
// and kept a 2 s cadence straight through a 60-90 s service restart.
// createPoller (core-utils.js) pauses while hidden and runs once on return,
// never overlaps itself, and backs off on failure.
//
// Run: cd tests/web && npm ci && node --test
import test from "node:test";
import assert from "node:assert/strict";
import { installDom } from "./helpers/portal-dom.mjs";

installDom();

const { createPoller } = await import("../../Portal/modules/core-utils.js");

/** Deterministic timers: nothing runs until the test advances the clock. */
function manualTimers() {
    let now = 0;
    let seq = 0;
    const pending = new Map();
    return {
        setTimeout(fn, ms) { const id = ++seq; pending.set(id, { at: now + ms, fn }); return id; },
        clearTimeout(id) { pending.delete(id); },
        delays() { return [...pending.values()].map((t) => t.at - now); },
        async advance(ms) {
            const until = now + ms;
            for (;;) {
                const due = [...pending.entries()].filter(([, t]) => t.at <= until).sort((a, b) => a[1].at - b[1].at)[0];
                if (!due) break;
                pending.delete(due[0]);
                now = due[1].at;
                due[1].fn();
                await flush();
            }
            now = until;
        },
    };
}

const flush = () => new Promise((r) => setImmediate(r));

function setHidden(hidden) {
    Object.defineProperty(document, "hidden", { value: hidden, configurable: true });
    Object.defineProperty(document, "visibilityState", { value: hidden ? "hidden" : "visible", configurable: true });
    document.dispatchEvent(new Event("visibilitychange"));
}

test("runs immediately, then every interval measured from the end of a tick", async () => {
    setHidden(false);
    const timers = manualTimers();
    let calls = 0;
    const p = createPoller(() => { calls += 1; }, { intervalMs: 1000, timers });
    p.start();
    await flush();
    assert.equal(calls, 1);
    await timers.advance(999);
    assert.equal(calls, 1);
    await timers.advance(1);
    assert.equal(calls, 2);
    await timers.advance(3000);
    assert.equal(calls, 5);
    p.stop();
    await timers.advance(10000);
    assert.equal(calls, 5, "stop() ends it for good");
});

test("never overlaps itself: a slow tick is not doubled by poke() or the timer", async () => {
    setHidden(false);
    const timers = manualTimers();
    let inFlight = 0;
    let maxInFlight = 0;
    let release;
    let calls = 0;
    const p = createPoller(() => {
        calls += 1;
        inFlight += 1;
        maxInFlight = Math.max(maxInFlight, inFlight);
        return new Promise((r) => { release = () => { inFlight -= 1; r(); }; });
    }, { intervalMs: 100, timers });
    p.start();
    await flush();
    p.poke();
    p.poke();
    await timers.advance(1000);
    assert.equal(calls, 1, "the tick still in flight is never started again");
    release();
    await flush();
    await timers.advance(100);
    assert.equal(calls, 2);
    assert.equal(maxInFlight, 1);
    p.stop();
});

test("pauses while hidden and runs once as soon as the page is visible again", async () => {
    setHidden(false);
    const timers = manualTimers();
    let calls = 0;
    const p = createPoller(() => { calls += 1; }, { intervalMs: 2000, timers });
    p.start();
    await flush();
    assert.equal(calls, 1);
    setHidden(true);
    await timers.advance(60000);
    assert.equal(calls, 1, "no request while the tab/WebView is in the background");
    assert.deepEqual(timers.delays(), [], "no timer is even armed while hidden");
    setHidden(false);
    await flush();
    assert.equal(calls, 2, "an immediate catch-up tick on return");
    await timers.advance(2000);
    assert.equal(calls, 3);
    p.stop();
});

test("a poller started while hidden waits for visibility", async () => {
    setHidden(true);
    const timers = manualTimers();
    let calls = 0;
    const p = createPoller(() => { calls += 1; }, { intervalMs: 500, timers });
    p.start();
    await timers.advance(5000);
    assert.equal(calls, 0);
    setHidden(false);
    await flush();
    assert.equal(calls, 1);
    p.stop();
});

test("backs off exponentially on failure, reports it, and snaps back on recovery", async () => {
    setHidden(false);
    const timers = manualTimers();
    let fail = true;
    const errors = [];
    let recovered = 0;
    const p = createPoller(() => { if (fail) throw new Error("box restarting"); }, {
        intervalMs: 1000, maxIntervalMs: 8000, timers,
        onError: (err, failures) => errors.push([err.message, failures]),
        onRecover: () => { recovered += 1; },
    });
    p.start();
    await flush();
    assert.deepEqual(timers.delays(), [2000]);
    await timers.advance(2000);
    assert.deepEqual(timers.delays(), [4000]);
    await timers.advance(4000);
    assert.deepEqual(timers.delays(), [8000]);
    await timers.advance(8000);
    assert.deepEqual(timers.delays(), [8000], "capped at maxIntervalMs");
    assert.deepEqual(errors.map((e) => e[1]), [1, 2, 3, 4]);
    fail = false;
    await timers.advance(8000);
    assert.equal(recovered, 1);
    assert.equal(p.failures, 0);
    assert.deepEqual(timers.delays(), [1000], "back to the base cadence");
    p.stop();
});

test("a tick may stop its own poller (self-stopping media sweeps)", async () => {
    setHidden(false);
    const timers = manualTimers();
    let calls = 0;
    const p = createPoller(() => { calls += 1; if (calls === 2) p.stop(); }, { intervalMs: 100, timers });
    p.start();
    await flush();
    await timers.advance(1000);
    assert.equal(calls, 2);
    assert.equal(p.running, false);
    p.start();
    await flush();
    assert.equal(calls, 3, "and start() revives it");
    p.stop();
});

test("the Portal's network pollers run on createPoller, not bare setInterval", async () => {
    const { readFileSync } = await import("node:fs");
    const { resolve } = await import("node:path");
    const { PORTAL } = await import("./helpers/portal-dom.mjs");
    const { stripComments } = await import("./helpers/sink-scan.mjs");
    const modules = ["ui-setup", "app-init", "task-manager", "chat-send", "cellular-manager",
        "cron-manager", "telephony-manager", "device-manager", "cu-interact"];
    const offenders = modules.filter((m) =>
        /\bsetInterval\s*\(/.test(stripComments(readFileSync(resolve(PORTAL, "modules", `${m}.js`), "utf8"))));
    assert.deepEqual(offenders, [], "a bare setInterval polls a hidden tab forever and overlaps slow requests");
});
