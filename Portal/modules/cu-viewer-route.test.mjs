// Unit tests for the CU viewer stream-vs-fallback decision logic
// (design doc 2026-07-23 §7.1, milestone M4 "Portal integration").
// chooseCuViewer is PURE — no DOM, no network — driven here with fake
// /cu/sessions payloads (shape: display.py to_public / browser_routes.py
// cu_sessions: {active, count, cap, sessions:[{session_id, operator,
// backend, width, height, display, live_view, view_url, started_at}]}).
//
// Run: node --test Portal/modules/cu-viewer-route.test.mjs
import test from "node:test";
import assert from "node:assert/strict";

import { chooseCuViewer } from "./cu-viewer-route.js";

/** Build a fake /cu/sessions session entry (to_public shape). */
function session(id, overrides = {}) {
    return {
        session_id: id,
        operator: "Brandon",
        backend: "anthropic",
        width: 1280,
        height: 720,
        display: ":100",
        live_view: true,
        view_url: `/cu/view/${id}`,
        started_at: 1753200000,
        ...overrides,
    };
}

/** Build a fake /cu/sessions payload around a sessions array. */
function payload(sessions) {
    return { active: sessions.length > 0, count: sessions.length, cap: 3, sessions };
}

// ── degraded / empty payloads → fallback ─────────────────────────────────

test("null status (fetch failed) → fallback, never a dead panel", () => {
    assert.deepEqual(chooseCuViewer(null), { mode: "fallback", reason: "no-sessions" });
    assert.deepEqual(chooseCuViewer(undefined), { mode: "fallback", reason: "no-sessions" });
});

test("empty sessions list (native mode / nothing running) → fallback", () => {
    assert.deepEqual(chooseCuViewer(payload([])),
        { mode: "fallback", reason: "no-sessions" });
});

test("malformed payload (sessions not an array) → fallback", () => {
    assert.deepEqual(chooseCuViewer({ active: true, sessions: "wat" }),
        { mode: "fallback", reason: "no-sessions" });
    assert.deepEqual(chooseCuViewer({ sessions: null }),
        { mode: "fallback", reason: "no-sessions" });
});

// ── generic pick (no sessionId): first streamable session ────────────────

test("single live virtual session → stream it", () => {
    const s = session("abc");
    const choice = chooseCuViewer(payload([s]));
    assert.equal(choice.mode, "stream");
    assert.equal(choice.session.session_id, "abc");
    assert.equal(choice.session.view_url, "/cu/view/abc");
});

test("skips non-streamable sessions to find a live one", () => {
    const dead = session("dead", { live_view: false });
    const live = session("live");
    const choice = chooseCuViewer(payload([dead, live]));
    assert.equal(choice.mode, "stream");
    assert.equal(choice.session.session_id, "live");
});

test("sessions exist but none streamable (no websockify box) → fallback", () => {
    const choice = chooseCuViewer(payload([
        session("a", { live_view: false }),
        session("b", { live_view: false }),
    ]));
    assert.deepEqual(choice, { mode: "fallback", reason: "stream-unavailable" });
});

test("live_view true but view_url missing → not streamable", () => {
    const choice = chooseCuViewer(payload([session("a", { view_url: "" })]));
    assert.deepEqual(choice, { mode: "fallback", reason: "stream-unavailable" });
});

// ── targeted pick (sessionId from the drawer / cu_session SSE) ───────────

test("sessionId match with live stream → stream THAT session, not the first", () => {
    const first = session("first");
    const mine = session("mine", { backend: "openai" });
    const choice = chooseCuViewer(payload([first, mine]), { sessionId: "mine" });
    assert.equal(choice.mode, "stream");
    assert.equal(choice.session.session_id, "mine");
});

test("sessionId match but stream unavailable → fallback", () => {
    const mine = session("mine", { live_view: false });
    const choice = chooseCuViewer(payload([session("other"), mine]), { sessionId: "mine" });
    assert.deepEqual(choice, { mode: "fallback", reason: "stream-unavailable" });
});

test("sessionId not listed (ended/reaped/native) → fallback, NEVER a different session", () => {
    const choice = chooseCuViewer(payload([session("someone-else")]),
        { sessionId: "mine" });
    assert.deepEqual(choice, { mode: "fallback", reason: "session-not-listed" });
});

// ── device routing: remote targets have no local virtual display ─────────

test("remote device (VNC/Android target) → fallback even with live sessions", () => {
    const choice = chooseCuViewer(payload([session("abc")]),
        { deviceId: "fold-vnc" });
    assert.deepEqual(choice, { mode: "fallback", reason: "remote-device" });
});

test("local device ids ('blackbox', 'local', empty) stream normally", () => {
    for (const deviceId of ["blackbox", "local", "", null, undefined]) {
        const choice = chooseCuViewer(payload([session("abc")]), { deviceId });
        assert.equal(choice.mode, "stream", `deviceId=${String(deviceId)}`);
    }
});
